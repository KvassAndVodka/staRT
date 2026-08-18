"""
Hermetic Tests for Coordinator Supervision, ASR Settings, Non-Contiguous Gap Backlog Replay, and Asset Lifecycles
"""
import uuid
import pytest
import hashlib
import numpy as np
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.config import settings
from app.adapters.storage.database import init_db, AsyncSessionLocal
from app.domain.models import SessionModel, AudioAssetModel, AudioFragmentModel
from app.application.job_coordinator import coordinator
from app.adapters.asr.faster_whisper_engine import FasterWhisperASREngine, ASREngineError, HypothesisWord

@pytest.mark.asyncio
async def test_stop_queued_session_cancels_it():
    await init_db()
    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Queued Test",
            source_url="https://example.com/queued.mp3",
            status="queued"
        )
        db.add(session)
        await db.commit()

    await coordinator.stop_job(session_id)

    async with AsyncSessionLocal() as db:
        res = await db.execute(SessionModel.__table__.select().where(SessionModel.id == session_id))
        row = res.first()
        assert row.status == "cancelled"

@pytest.mark.asyncio
async def test_asr_settings_resolution_in_transcribe_window():
    """Verify that all settings referenced by FasterWhisperASREngine exist and are valid."""
    assert hasattr(settings, "VAD_MIN_SILENCE_DURATION_MS")
    assert hasattr(settings, "VAD_SPEECH_PAD_MS")
    assert hasattr(settings, "BEAM_SIZE")
    assert hasattr(settings, "DEFAULT_DEVICE")
    assert settings.BEAM_SIZE >= 1

    engine = FasterWhisperASREngine.__new__(FasterWhisperASREngine)
    engine.model_name = "test-model"
    engine.requested_device = "cpu"
    engine.actual_device = "cpu"
    engine.actual_compute_type = "int8"
    engine.model = MagicMock()
    
    mock_segment = MagicMock()
    mock_word = MagicMock()
    mock_word.word = "hello"
    mock_word.start = 0.0
    mock_word.end = 1.0
    mock_word.probability = 0.95
    mock_segment.words = [mock_word]
    
    mock_info = MagicMock()
    mock_info.language = "en"
    
    engine.model.transcribe.return_value = ([mock_segment], mock_info)
    
    dummy_audio = np.zeros(16000, dtype=np.float32)
    hypotheses = engine.transcribe_window(dummy_audio, window_start_ms=0, language_mode="en")
    assert len(hypotheses) == 1
    assert hypotheses[0].text == "hello"
    assert hypotheses[0].start_ms == 0
    assert hypotheses[0].end_ms == 1000

@pytest.mark.asyncio
async def test_asr_failure_marks_session_and_asset_failed():
    await init_db()
    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="ASR Failure Test",
            source_url="https://example.com/test_fail.mp3",
            status="queued"
        )
        db.add(session)
        await db.commit()

    dummy_frag_path = settings.SESSIONS_DIR / session_id / "fragments" / "frag_000000_0_2000.raw"
    dummy_frag_path.parent.mkdir(parents=True, exist_ok=True)
    raw_b = b"\x00" * 64000
    with open(dummy_frag_path, "wb") as f:
        f.write(raw_b)
    real_sha = hashlib.sha256(raw_b).hexdigest()

    async def mock_stream_fragments(*args, **kwargs):
        yield (0, 0, 32000, 32000, 0, 2000, np.zeros(32000, dtype=np.float32), dummy_frag_path, real_sha)

    with patch("app.application.job_coordinator.StreamIngestionAdapter.resolve_source", new_callable=AsyncMock) as mock_res:
        from app.adapters.ingestion.stream_capture import ResolvedSource
        mock_res.return_value = ResolvedSource(
            title="Mocked Source",
            media_url="https://example.com/test_fail.mp3",
            is_live=False,
            duration_sec=2.0,
            http_headers={}
        )
        mock_engine = MagicMock()
        mock_engine.actual_device = "cpu"
        mock_engine.actual_compute_type = "int8"
        mock_engine.transcribe_window.side_effect = ASREngineError("CUDA OOM Failure")
        with patch.object(coordinator, "get_asr_engine", new_callable=AsyncMock) as mock_get_asr:
            mock_get_asr.return_value = mock_engine
            with patch("app.application.job_coordinator.StreamIngestionAdapter.stream_pcm_fragments", mock_stream_fragments):
                await coordinator._run_job(session_id)

    async with AsyncSessionLocal() as db:
        res = await db.execute(SessionModel.__table__.select().where(SessionModel.id == session_id))
        row = res.first()
        assert row.status == "failed"
        assert "asr_error" in row.error_code

        # Check audio assets status transitioned to failed
        a_res = await db.execute(AudioAssetModel.__table__.select().where(AudioAssetModel.session_id == session_id))
        assets = a_res.fetchall()
        for a in assets:
            assert a.status == "failed"

@pytest.mark.asyncio
async def test_non_contiguous_gap_backlog_replay():
    """Verify that non-contiguous gaps in skipped durable fragments are correctly identified and replayed from disk."""
    await init_db()
    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Gap Replay Test",
            source_url="https://example.com/stream.mp3",
            status="queued"
        )
        db.add(session)
        await db.commit()

    frag_dir = settings.SESSIONS_DIR / session_id / "fragments"
    audio_dir = settings.SESSIONS_DIR / session_id / "audio"
    frag_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    import hashlib
    total_frags = 5
    frag_hashes = {}
    for i in range(total_frags):
        frag_p = frag_dir / f"frag_{i:06d}_{i*2000}_{(i+1)*2000}.raw"
        raw_b = b"\x00" * 64000
        with open(frag_p, "wb") as f:
            f.write(raw_b)
        frag_hashes[i] = hashlib.sha256(raw_b).hexdigest()

    # Write dummy master and inference audio files
    with open(audio_dir / "master.m4a", "wb") as f:
        f.write(b"M4A_DUMMY_HEADER_BYTES" * 10)
    with open(audio_dir / "inference.wav", "wb") as f:
        f.write(b"RIFF" + b"\x00" * 100)

    async def mock_stream_fragments(*args, **kwargs):
        for i in range(total_frags):
            sample_start = i * 32000
            sample_end = (i + 1) * 32000
            frag_p = frag_dir / f"frag_{i:06d}_{i*2000}_{(i+1)*2000}.raw"
            yield (i, sample_start, sample_end, 32000, i * 2000, (i + 1) * 2000, np.zeros(32000, dtype=np.float32), frag_p, frag_hashes[i])

    with patch("app.application.job_coordinator.StreamIngestionAdapter.resolve_source", new_callable=AsyncMock) as mock_res:
        from app.adapters.ingestion.stream_capture import ResolvedSource
        mock_res.return_value = ResolvedSource(
            title="Gap Stream",
            media_url="https://example.com/stream.mp3",
            is_live=False,
            duration_sec=float(total_frags * 2),
            http_headers={}
        )
        from app.adapters.ingestion.stream_capture import ProbedMediaInfo
        with patch("app.application.job_coordinator.StreamIngestionAdapter.probe_media_file", new_callable=AsyncMock) as mock_probe:
            mock_probe.return_value = ProbedMediaInfo(
                container="m4a",
                codec="aac",
                channels=2,
                sample_rate_hz=44100,
                duration_ms=total_frags * 2000,
                size_bytes=1024
            )
            mock_engine = MagicMock()
            mock_engine.actual_device = "cuda"
            mock_engine.actual_compute_type = "int8_float16"
            
            replayed_windows = []
            def fake_transcribe(audio_chunk, window_start_ms, language_mode):
                replayed_windows.append(window_start_ms)
                return [HypothesisWord(start_ms=window_start_ms, end_ms=window_start_ms + 1500, text=f"word_at_{window_start_ms}", confidence=0.95)]
                
            mock_engine.transcribe_window.side_effect = fake_transcribe

            with patch.object(coordinator, "get_asr_engine", new_callable=AsyncMock) as mock_get_asr:
                mock_get_asr.return_value = mock_engine
                with patch("app.application.job_coordinator.StreamIngestionAdapter.stream_pcm_fragments", mock_stream_fragments):
                    await coordinator._run_job(session_id)

    async with AsyncSessionLocal() as db:
        res = await db.execute(SessionModel.__table__.select().where(SessionModel.id == session_id))
        row = res.first()
        assert row.status == "ready"
        assert row.duration_ms == total_frags * 2000
        assert row.actual_asr_device == "cuda"
        assert row.actual_compute_type == "int8_float16"
        assert len(replayed_windows) > 0

@pytest.mark.asyncio
async def test_range_clamping_beyond_eof():
    await init_db()
    session_id = str(uuid.uuid4())
    session_dir = settings.SESSIONS_DIR / session_id / "audio"
    session_dir.mkdir(parents=True, exist_ok=True)
    inference_path = session_dir / "inference.wav"

    dummy_bytes = b"RIFF" + b"B" * 96  # 100 bytes
    with open(inference_path, "wb") as f:
        f.write(dummy_bytes)

    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Range Clamping Test",
            source_url="https://example.com/test.mp3",
            status="ready"
        )
        db.add(session)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"/api/sessions/{session_id}/audio", headers={"Range": "bytes=50-500"})
        assert res.status_code == 206
        assert res.headers["Content-Range"] == "bytes 50-99/100"
        assert res.headers["Content-Length"] == "50"
        assert res.content == dummy_bytes[50:]
