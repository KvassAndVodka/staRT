# staRT implementation review

**Review date:** 2026-08-18  
**Reviewed against:** `live-transcription-service-spec-plan.md` draft v1.4  
**Scope:** Current working tree after the durability/recovery corrections

## Verdict

The immediate correctness blockers from the previous review are fixed. The runtime now has sample-exact work coordinates, a real restart-recovery path, fenced renewable leases, append-only attempt history, transactional outbox records, explicit SQLite migration, enforced foreign keys, and hermetic tests.

Milestone 2's durable scheduler, fragment publication boundary, and multi-epoch stall/reconnect recovery are now implemented and covered by adversarial tests. Protocol-level PTS resets and DVR jumps still require a timestamp-bearing demux signal; the current raw-PCM pipe cannot infer them reliably.

The repository began without Git history, so this review describes the initial application snapshot rather than an incremental commit diff.

## Verification

| Check | Result |
| --- | --- |
| Python compilation | **Pass** |
| Backend suite | **Pass:** 56 tests in 3.66 seconds |
| Frontend lint | **Pass** |
| Frontend type-check | **Pass** |
| Frontend production build | **Pass** |
| Retained-schema migration fixture | **Pass:** exact 1,001-sample backfill plus physical check/unique constraints |
| Disposable copy of application DB | **Pass:** migration version 2 installed; physical unique indexes present |

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

### Smaller fixes

- Record-only tests hold capture open and prove ASR claims stop until EOF drain.
- Retained inference work now returns an observable success/failure outcome after caller cancellation.
- An unknown FFmpeg exit state is a failure rather than implicit success.
- The live client no longer reconnects on every status change. It deduplicates stable outbox IDs and reloads a durable REST snapshot after reconnect.
- Live session detail can reconstruct current words from the latest durable reconciler snapshot before final transcript rows exist.

## Remaining work, in implementation order

### P1 — Protocol timestamp discontinuity signals

Wall-clock stalls and reconnects now produce explicit epochs and gaps. FFmpeg's normalized raw-PCM stdout does not expose input packet PTS, so an HLS/DASH discontinuity, timestamp reset, or DVR jump that does not stall can still pass without an epoch boundary.

Add a timestamp-bearing demux/progress side channel rather than inferring PTS from byte arrival. Normalize that signal into the existing `SourceReconnecting` / `StreamDiscontinuity` contract, populate `source_pts_start` / `source_pts_end`, and add fixtures for backward PTS reset, forward DVR jump, and `EXT-X-DISCONTINUITY`. Do not enable this from brittle stderr string matching.

### P1 — Redirect-safe SSRF enforcement

Current hostname validation rejects resolved private/local addresses, but `yt-dlp` and FFmpeg perform their own requests and redirects. DNS can change between validation and connection, and an extractor can follow a public URL to a private target before the final media URL is checked.

Put network acquisition behind one policy-enforcing layer that validates every redirect hop, rejects DNS failures, resolves all A/AAAA records, blocks private/reserved/link-local ranges, and pins the approved address for the actual connection. Do not pass unrestricted extractor headers across origins. Add tests for redirect-to-loopback, redirect-to-link-local metadata, mixed public/private DNS answers, IPv4-mapped IPv6, and DNS rebinding.

### P1 — Faithful master asset

The master output is always re-encoded to AAC at 192 kbps. That is a useful playback derivative, not a source-faithful master.

Separate the assets:

- preserve/download or remux the source audio without transcoding when the container permits;
- record the original codec/container/hash and any transform provenance;
- create a separate playback derivative when browser compatibility requires AAC; and
- keep the 16 kHz mono WAV strictly as the inference derivative.

Test copy/remux and transcode fallback paths with real small fixtures and verify probe metadata.

### P2 — Multi-client event delivery semantics

Stable IDs, client deduplication, and REST snapshot recovery prevent transcript corruption and repair reconnect gaps. The outbox still records one global `published_at`, not delivery or acknowledgement per client. A second late client receives a snapshot rather than the original event sequence.

If exact event replay is required, add a durable monotonic session sequence, a `since_sequence` WebSocket handshake, bounded replay, and per-client acknowledgement/cursor semantics. If snapshots are the intended contract, state that explicitly and compact old outbox rows after a checkpoint.

### P2 — Real-process acceptance coverage

Most ingestion/recovery tests use mocked extractors, FFmpeg, and ASR. Add a small checked-in media fixture and run FFmpeg/FFprobe end to end in CI. Keep external URLs and model downloads out of the default suite.

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

Add focused tests for the remaining P1 work:

- packet-PTS reset and DVR-jump epoch boundaries;
- redirect/DNS-rebinding SSRF cases; and
- source-preserving master/remux plus playback-transcode fallback.
