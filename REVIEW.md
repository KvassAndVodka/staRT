# staRT implementation review

**Review date:** 2026-08-20
**Reviewed against:** `live-transcription-service-spec-plan.md` draft v1.4  
**Scope:** Current working tree after the durability/recovery corrections

## Verdict

The immediate correctness blockers from the previous review are fixed. The runtime now has sample-exact work coordinates, a real restart-recovery path, fenced renewable leases, append-only attempt history, transactional outbox records, connection-time SSRF enforcement, source-faithful master handling, packet-level presentation timestamps, explicit SQLite migration, enforced foreign keys, and hermetic tests.

Milestone 2's durable scheduler, fragment publication boundary, multi-epoch stall/reconnect recovery, and protocol timestamp boundaries are now implemented and covered by adversarial tests. The remaining work is product hardening rather than a known P1 data-integrity blocker.

The repository began without Git history, so this review describes the initial application snapshot rather than an incremental commit diff.

## Verification

| Check | Result |
| --- | --- |
| Python compilation | **Pass** |
| Backend suite | **Pass:** 67 tests |
| Frontend lint | **Pass** |
| Frontend type-check | **Pass** |
| Frontend production build | **Pass** |
| Retained-schema migration fixture | **Pass:** exact 1,001-sample backfill, asset provenance, and physical constraints |
| Disposable copy of application DB | **Pass:** migration version 3 installed; physical unique indexes present |

The backend and frontend commands were run outside the restricted command sandbox because `aiosqlite` and Node worker processes cannot complete reliably inside it.

## Previous blockers: resolved

### Sample-exact ledger and tails

- `InferenceWindowModel` now stores target/context sample coordinates and sample rate.
- Scheduling advances in integer samples and creates a final tail window ending at the exact durable frontier.
- `VerifiedAudioWindowAssembler.assemble_samples()` never performs a millisecond round-trip.
- Durable fragments require aligned byte length, exact sample count, and a matching SHA-256 digest.
- Readiness compares exact contiguous fragment and active-revision window coverage.
- Tests cover 1, 15, 16, 17, 1,001, and 31,999-sample assembler tails and 1, 319, 320, 321, 23,999, 24,000, and 24,001-sample scheduler frontiers.

### Actual startup recovery

- Interrupted sessions with verified durable input enter `recovering_source` and are selected by the normal single-job arbiter before new captures.
- Recovery verifies every fragment before loading ASR, recreates missing work idempotently, restores the latest reconciler snapshot, drains only unfinished windows, rebuilds the inference WAV, finalizes existing assets, and reaches `ready` without invoking capture.
- Expired attempts are marked `superseded` instead of being silently overwritten.
- A missing/corrupt recovery file fails before model loading.

### Runtime epochs and discontinuities

- Live reads that exceed the configured stall threshold publish `source.reconnecting` without stopping capture.
- When audio resumes, capture closes the prior epoch, inserts an explicit `TimelineGapModel`, resets sample/sequence coordinates for the next epoch, and publishes `source.reconnected`.
- Window milliseconds retain each epoch's source-time offset while sample coordinates remain local and exact.
- Claims, reconciler snapshots, and startup recovery are ordered by `(stream_epoch, ordinal)`.
- Provisional words are committed at an epoch boundary before new-epoch hypotheses are reconciled.
- Readiness validates exact fragment/window coverage inside every epoch and requires one correctly mapped durable gap between adjacent epochs.
- Timestamped exports include `[audio unavailable during stream interruption]`; JSON includes source and wall-clock gap mappings.
- Tests cover live reconnect state, two-epoch finalization, restart recovery without recapture, readiness failure when the gap record is missing, and a terminal outage that remains an open durable gap instead of being finalized as ready.

### Protocol timestamp discontinuities

- FFmpeg receives `-copyts` and writes structured `framehash` packet records to a dedicated inherited file descriptor. Diagnostic stderr is never interpreted as timing data.
- The normalized PCM stream and timing stream come from one `asplit`, so each timing record describes the same decoded and resampled samples sent to inference.
- A publication barrier waits for timing packets to cover each raw PCM read before any fragment is written or yielded.
- The timing parser requires an exact `1 / sample_rate` time base and rejects malformed, empty, misaligned, or sample-count-inconsistent packets.
- A read that crosses a PTS jump is split at the exact packet sample boundary. Forward jumps create a positive `source_dvr_jump` interval; backward resets create a zero-duration `source_pts_reset` epoch boundary without inventing unavailable audio.
- Every captured fragment now persists its normalized source PTS start and exclusive end. The fragment sample rate defines the PTS time base.
- Lossless JSON retains both positive gaps and zero-duration reset boundaries. Subtitle and prose exports add an unavailable-audio marker only for positive-duration gaps.
- Tests cover parser splits, adapter-level forward and backward boundaries, zero-duration readiness validation, and a real discontinuous HLS playlist processed through FFmpeg and the policy proxy.

### Lease fencing and durable result commit

- Each claim creates a unique attempt ID and append-only `inference_attempts` record.
- A heartbeat renews the lease while assembly/ASR runs.
- Completion and failure updates require the exact owner, active attempt, and `running` state.
- A stale worker that loses its lease cannot change the window or publish an event.
- Window completion, immutable attempt output, reconciler snapshot, and idempotently keyed outbox events commit in one transaction.
- Events publish only after commit and include a stable `event_id`.

### Readiness

`ready` now requires all of the following for the active processing revision:

- exactly one ready master asset and one ready inference asset;
- verified contiguous durable fragments to the exact final sample;
- contiguous succeeded target windows to the same exact sample;
- committed attempt IDs, manifests, hypotheses, and reconciler snapshots;
- no active owner or attempt; and
- no unpublished outbox event.

The final transcript and the `ready` state commit only after these checks pass.

### Schema and test isolation

- Schema version 2 rebuilds retained SQLite tables instead of relying on `create_all()` to alter them.
- Historical fragment coordinates are derived from verified file bytes. Unverifiable rows are quarantined as `corrupt`; migration errors are not swallowed.
- Migration checks physical SQLite unique keys through index metadata, avoiding an intermittent SQLAlchemy reflection omission.
- `PRAGMA foreign_keys=ON` is enabled on every application connection, and purge cascade behavior is tested.
- Tests use a temporary database and storage tree. API tests do not leak recorder tasks into later tests.

### Crash-atomic fragment publication

- PCM bytes are written to a unique staging file in the destination directory.
- The staged file is flushed, `fsync()`ed, then re-read to verify exact size and SHA-256.
- Publication uses an atomic rename and an `fsync()` barrier on the containing directory before the adapter yields the fragment for ledger insertion.
- Existing final paths are rejected rather than overwritten.
- Startup deletes abandoned staging files, quarantines unreferenced final files, and marks/quarantines files that contradict durable ledger rows.
- Tests inject rename and directory-sync failures and model persisted crash states before rename and before database insertion.

### Redirect-safe SSRF enforcement

- `yt-dlp` and FFmpeg use one loopback-only policy proxy for all HTTP and HTTPS acquisition.
- The proxy validates every redirect, manifest, segment, and reconnect destination before it connects.
- DNS failures are fatal. The policy rejects a hostname if any A or AAAA answer is non-public.
- The proxy connects to the approved numeric address, which prevents a second DNS lookup from changing the destination.
- IPv4-mapped IPv6, loopback, private, reserved, link-local, multicast, and mixed public/private answers are rejected.
- `NO_PROXY` is cleared for the child process so environment settings cannot bypass the policy.
- Cookies and authorization headers stay on the extractor source origin. Cross-origin media requests receive only non-sensitive headers.
- RTMP and RTMPS inputs are rejected because FFmpeg cannot route those protocols through this HTTP policy boundary.
- Hop-by-hop headers and any header named by `Connection` are removed before forwarding.
- Tests cover DNS failure, mixed answers, IPv4-mapped loopback, redirect-to-loopback, cross-origin credential removal, and hop-by-hop header stripping.

### Source-faithful master and playback derivative

- Known audio codecs are copied without transcoding into a Matroska master.
- An unknown codec uses an explicit lossless FLAC fallback instead of silently becoming a lossy AAC master.
- Browser playback is a separate 192 kbps AAC derivative. Inference remains a separate 16 kHz mono PCM derivative.
- Asset rows record the source codec/container, transform operation, target format, and derivation link.
- Session playback prefers the browser derivative. Downloads retain accurate media types for master and derivative containers.
- Schema version 3 adds retained asset provenance without rebuilding unrelated tables.
- Tests cover remux selection, lossless fallback, FFmpeg output arguments, lineage, API provenance, and playback selection.
- A real FFmpeg/FFprobe acceptance test routes a generated WAV through the policy proxy and verifies all three output codecs.

### Smaller fixes

- Record-only tests hold capture open and prove ASR claims stop until EOF drain.
- Retained inference work now returns an observable success/failure outcome after caller cancellation.
- An unknown FFmpeg exit state is a failure rather than implicit success.
- The live client no longer reconnects on every status change. It deduplicates stable outbox IDs and reloads a durable REST snapshot after reconnect.
- Live session detail can reconstruct current words from the latest durable reconciler snapshot before final transcript rows exist.

## Remaining work, in implementation order

### P2 — Multi-client event delivery semantics

Stable IDs, client deduplication, and REST snapshot recovery prevent transcript corruption and repair reconnect gaps. The outbox still records one global `published_at`, not delivery or acknowledgement per client. A second late client receives a snapshot rather than the original event sequence.

If exact event replay is required, add a durable monotonic session sequence, a `since_sequence` WebSocket handshake, bounded replay, and per-client acknowledgement/cursor semantics. If snapshots are the intended contract, state that explicitly and compact old outbox rows after a checkpoint.

### P2 — Broader real-process acceptance coverage

The default suite now runs FFmpeg and FFprobe end to end against a generated local WAV and a generated discontinuous HLS playlist through the policy proxy. Add checked-in fixtures for more compressed codecs, DASH, encrypted-but-key-accessible HLS, malformed manifests, and longer discontinuity chains. Keep external URLs and model downloads out of the default suite.

## Acceptance gate for the next review

Preserve:

```bash
cd backend
.venv/bin/python -m compileall -q app tests
.venv/bin/pytest -q

cd ../frontend
npm run lint
npx tsc --noEmit
npm run build
```

For the next hardening pass, add bounded multi-client event replay or explicitly document snapshot recovery as the final delivery contract. Extend real-process fixtures without adding external URLs or model downloads to the default suite.
