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
| **Milestone 4: Diarization & Overlap Pipeline** | **Core contracts verified** | Optional Community-1 adapter; lazy model loading; CUDA-to-CPU retry; sequential ASR release; multi-epoch source-time mapping; maximum-overlap speaker identity matching; transactional activities, overlap regions, word attribution, and turn rebuilds; seekable overlap review. The hardware benchmark remains planned. |
| **Milestone 5: Uncertainty Queue & Glossary Engine** | *Planned (Phase 3)* | "Next issue" keyboard review traversal across low-confidence words/gaps, Tagalog/Cebuano domain glossary hints. |
| **Milestone 6: Continuous Learning Loop & LoRA Fine-Tuning** | *Planned (Phase 5)* | Immutable correction event export (versioned JSONL/RTTM dataset snapshots), Whisper LoRA fine-tuning runner, Model Registry. |

---

## Quick Start

### 1. Prerequisites
- Linux with FFmpeg installed (`ffmpeg -version`)
- Python 3.11 / 3.12 (managed via `uv` or system Python)
- Node.js 24 LTS and npm 11. Node.js 26 with npm 11 is checked for forward compatibility.
- NVIDIA GPU (RTX 3050 Ti Mobile 4GB or RTX 3050 6GB recommended; CPU fallback supported)

### 2. Run Locally

```bash
./run_dev.sh
```

- **Frontend**: [http://localhost:3000](http://localhost:3000)
- **Backend API & Swagger Docs**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

### 3. Run Automated Tests

```bash
# Backend test suite (90 tests, including local FFmpeg/FFprobe and HLS acceptance)
cd backend && .venv/bin/pytest -v

# Frontend lint, typecheck, & production build
cd ../frontend && npm run lint && npm run typecheck && npm run build
```

GitHub Actions runs the backend suite on Python 3.11 and 3.12.
It requires frontend checks on Node.js 24 LTS.
The workflow also runs a non-blocking Node.js 26 typecheck and production build until Node.js 26 enters LTS.

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

### Enable Community-1

Final diarization is optional. The default install does not import `pyannote.audio` or load a speaker model.

The [Community-1 model](https://huggingface.co/pyannote/speaker-diarization-community-1) requires a Hugging Face account, accepted model conditions, and an access token.
Install the optional dependency group from the backend directory:

```bash
uv sync --extra dev --extra diarization
```

Use the token only in a separate model-acquisition command:

```bash
HF_TOKEN=hf_your_token .venv/bin/hf download pyannote/speaker-diarization-community-1 \
  --local-dir data/models/pyannote-community-1
unset HF_TOKEN
```

Do not put the token in the service environment.
Do not commit the token to the repository.
Use `--revision <commit>` in the acquisition command to pin a model revision.

Enable the local model after the acquisition command succeeds:

```bash
export START_ENABLE_FINAL_DIARIZATION=true
```

The default model source is `backend/data/models/pyannote-community-1`.
Set a different local file or directory when needed:

```bash
export START_ENABLE_FINAL_DIARIZATION=true
export START_FINAL_DIARIZATION_MODEL_SOURCE=/absolute/path/to/community-1
```

The service rejects a missing local model.
It forces Hugging Face offline mode and never passes a token to pyannote.

The adapter requests CUDA by default.
If CUDA initialization or inference fails, it reloads the model and retries once on CPU.
Set `START_FINAL_DIARIZATION_DEVICE=cpu` to always use CPU.

The service disables [pyannote usage telemetry](https://github.com/pyannote/pyannote-audio#telemetry) and Hugging Face telemetry by default.
Set `START_FINAL_DIARIZATION_TELEMETRY=true` to enable pyannote usage metrics.

The service accepts only `START_DEFAULT_DIARIZATION_MODEL` from session requests while final diarization is enabled.
This check prevents a session payload from selecting an untrusted remote model.
The default model ID is `pyannote-community-1`.

An enabled adapter is part of finalization.
The session fails if the dependency, local model, or model output is invalid.
The service keeps final diarization disabled until the operator completes this setup.
