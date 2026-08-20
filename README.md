# staRT — Local Transcript Service

Local-first live transcription and multi-speaker review platform for public audio/video URLs and live media streams.

Built against [`live-transcription-service-spec-plan.md`](./live-transcription-service-spec-plan.md).

---

## Milestone Status & Verified Capabilities

| Milestone | Status | Verified Capabilities & Tested Contracts |
| :--- | :---: | :--- |
| **Milestone 1: Ingestion & Three Audio Assets** | **Core contracts verified** | Connection-time SSRF policy proxy for every HTTP(S) redirect and media request; DNS pinning; same-origin credential forwarding; source-faithful Matroska remux or explicit lossless FLAC fallback; separate AAC playback and normalized 16 kHz mono inference derivatives with provenance; FFprobe metadata; Range audio; exports; Trash/Restore/Purge with enforced SQLite cascades. |
| **Milestone 2: Durable Interval Scheduler & Verified Assembly** | **Core contracts verified** | Exact integer-sample fragment and work ledgers; crash-atomic fragment publication with startup quarantine; arbitrary tail preservation; SHA/size/alignment verification; renewable fenced leases; append-only attempts; transactional outbox; active-revision readiness proof; restart recovery without recapture; explicit multi-epoch stall/reconnect/PTS boundaries with source/wall mappings; structured FFmpeg packet timing with a sample-boundary publication barrier; schema-v3 migration; hermetic adversarial tests. |
| **Milestone 3: Modern Web Client** | **Core contracts verified** | Next.js 16 App Router UI with real-time transcript updates, stable event-ID deduplication, reconnect snapshot recovery, configurable API/audio endpoints, speaker renaming, turn editing, exports, and history/trash management. Durable sequence replay for multiple late clients remains optional follow-up work. |
| **Milestone 4: Diarization & Overlap Pipeline** | *Planned (Phase 2)* | Pyannote Community-1 offline finalization pass, sequential GPU memory management, multi-speaker overlap detection. |
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
# Backend test suite (67 tests, including local FFmpeg/FFprobe and HLS acceptance)
cd backend && .venv/bin/pytest -v

# Frontend lint, typecheck, & production build
cd ../frontend && npm run lint && npx tsc --noEmit && npm run build
```
