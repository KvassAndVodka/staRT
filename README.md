# staRT — Local Transcript Service

Local-first live transcription and multi-speaker review platform for public audio/video URLs and live media streams.

Built against [`live-transcription-service-spec-plan.md`](./live-transcription-service-spec-plan.md).

---

## Milestone Status & Verified Capabilities

| Milestone | Status | Verified Capabilities & Tested Contracts |
| :--- | :---: | :--- |
| **Milestone 1: Ingestion & Three Audio Assets** | **Core contracts verified** | Connection-time SSRF policy proxy for every HTTP(S) redirect and media request; DNS pinning; same-origin credential forwarding; source-faithful Matroska remux or explicit lossless FLAC fallback; separate AAC playback and normalized 16 kHz mono inference derivatives with provenance; FFprobe metadata; Range audio; exports; Trash/Restore/Purge with enforced SQLite cascades. |
| **Milestone 2: Durable Interval Scheduler & Verified Assembly** | **Core contracts verified** | Exact integer-sample fragment and work ledgers; crash-atomic fragment publication with startup quarantine; arbitrary tail preservation; SHA/size/alignment verification; renewable fenced leases; append-only attempts; transactional outbox; active-revision readiness proof; restart recovery without recapture; explicit multi-epoch stall/reconnect/PTS boundaries with source/wall mappings; structured FFmpeg packet timing with a sample-boundary publication barrier; schema-v5 migration; hermetic adversarial tests. |
| **Milestone 3: Modern Web Client** | **Core contracts verified** | Next.js 16 App Router UI with real-time transcript updates, durable per-session sequences, bounded multi-client replay, snapshot fallback, stable event-ID deduplication, configurable API/audio endpoints, speaker renaming, turn editing, exports, and history/trash management. |
| **Milestone 4: Diarization & Overlap Pipeline** | **Foundation verified** | Replaceable final-diarization port; sequential ASR release; multi-epoch source-time mapping; maximum-overlap speaker identity matching; transactional activities, overlap regions, word attribution, and turn rebuilds; seekable overlap review. The gated Community-1 adapter and hardware benchmark remain planned. |
| **Milestone 5: Uncertainty Queue & Glossary Engine** | *Planned (Phase 3)* | "Next issue" keyboard review traversal across low-confidence words/gaps, Tagalog/Cebuano domain glossary hints. |
| **Milestone 6: Continuous Learning Loop & LoRA Fine-Tuning** | *Planned (Phase 5)* | Immutable correction event export (versioned JSONL/RTTM dataset snapshots), Whisper LoRA fine-tuning runner, Model Registry. |

---

## Quick Start

### 1. Prerequisites
- Linux with FFmpeg installed (`ffmpeg -version`)
- Python 3.11 / 3.12 (managed via `uv` or system Python)
- Node.js `>=20.9.0` and npm
- NVIDIA GPU (RTX 3050 Ti Mobile 4GB or RTX 3050 6GB recommended; CPU fallback supported)

### 2. Run Locally

```bash
./run_dev.sh
```

- **Frontend**: [http://localhost:3000](http://localhost:3000)
- **Backend API & Swagger Docs**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

### 3. Run Automated Tests

```bash
# Backend test suite (79 tests, including local FFmpeg/FFprobe and HLS acceptance)
cd backend && .venv/bin/pytest -v

# Frontend lint, typecheck, & production build
cd ../frontend && npm run lint && npx tsc --noEmit && npm run build
```

## Event Delivery Contract

The session detail response includes `event_sequence` and `event_replay_floor`.
The web client first reads this snapshot and then connects with `since_sequence`.

The server stores each event before delivery. It replays later events in sequence order and marks them with `replayed: true`.
The server sends `stream.snapshot_required` when it cannot safely replay the requested range.
This control event covers missing, expired, ahead, and discontinuous cursors.

`START_EVENT_REPLAY_LIMIT` sets the retained event tail. Its default value is 512 events per session.
Compaction removes only published events, so it does not discard pending outbox work.

## Final Speaker Pipeline

`DiarizationEngine` is a typed port for a replaceable whole-session model.
The coordinator runs an injected engine after it finalizes the inference audio and releases the ASR model.

Adapter timestamps use the contiguous inference-audio timeline.
The coordinator maps them through durable fragments onto the canonical source timeline.
This mapping preserves source gaps and splits activity at stream epoch boundaries.

The final database update is transactional.
It matches final clusters to existing speaker names by maximum temporal overlap.
It then replaces speaker activities, marks simultaneous speech, attributes non-overlap words, and rebuilds turns.
Words in unresolved mixed overlap have no speaker assignment.

The default install does not include gated diarization weights or `pyannote.audio`.
The production Community-1 adapter remains part of the hardware and dependency spike.
