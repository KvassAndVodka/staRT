"""
Tests for Audio Range Streaming RFC 7233 Compliance and Lifecycle Guards
"""
import uuid
import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.config import settings
from app.adapters.storage.database import init_db, AsyncSessionLocal
from app.domain.models import SessionModel, AudioAssetModel

@pytest.mark.asyncio
async def test_audio_range_requests():
    await init_db()
    session_id = str(uuid.uuid4())
    session_dir = settings.SESSIONS_DIR / session_id / "audio"
    session_dir.mkdir(parents=True, exist_ok=True)
    inference_path = session_dir / "inference.wav"

    # Write dummy 100-byte wav file
    dummy_bytes = b"RIFF" + b"A" * 96
    with open(inference_path, "wb") as f:
        f.write(dummy_bytes)

    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Audio Range Test",
            source_url="https://example.com/test.mp3",
            status="ready"
        )
        db.add(session)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Full content request (no Range)
        res_full = await ac.get(f"/api/sessions/{session_id}/audio")
        assert res_full.status_code == 200
        assert res_full.headers["Content-Length"] == "100"
        assert res_full.content == dummy_bytes

        # 2. Explicit range (bytes=0-9)
        res_partial = await ac.get(f"/api/sessions/{session_id}/audio", headers={"Range": "bytes=0-9"})
        assert res_partial.status_code == 206
        assert res_partial.headers["Content-Range"] == "bytes 0-9/100"
        assert res_partial.headers["Content-Length"] == "10"
        assert res_partial.content == dummy_bytes[:10]

        # 3. Open-ended range (bytes=50-)
        res_open = await ac.get(f"/api/sessions/{session_id}/audio", headers={"Range": "bytes=50-"})
        assert res_open.status_code == 206
        assert res_open.headers["Content-Range"] == "bytes 50-99/100"
        assert res_open.headers["Content-Length"] == "50"
        assert res_open.content == dummy_bytes[50:]

        # 4. Suffix range (bytes=-20)
        res_suffix = await ac.get(f"/api/sessions/{session_id}/audio", headers={"Range": "bytes=-20"})
        assert res_suffix.status_code == 206
        assert res_suffix.headers["Content-Range"] == "bytes 80-99/100"
        assert res_suffix.headers["Content-Length"] == "20"
        assert res_suffix.content == dummy_bytes[-20:]

        # 5. Out of bounds range (bytes=200-300) -> 416
        res_invalid = await ac.get(f"/api/sessions/{session_id}/audio", headers={"Range": "bytes=200-300"})
        assert res_invalid.status_code == 416
        assert res_invalid.headers["Content-Range"] == "bytes */100"


@pytest.mark.asyncio
async def test_session_audio_prefers_playback_derivative_and_exposes_provenance():
    session_id = str(uuid.uuid4())
    audio_dir = settings.SESSIONS_DIR / session_id / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    master_path = audio_dir / "master.mka"
    playback_path = audio_dir / "playback.m4a"
    inference_path = audio_dir / "inference.wav"
    master_path.write_bytes(b"source-faithful-master")
    playback_path.write_bytes(b"browser-playback")
    inference_path.write_bytes(b"RIFF" + b"normalized" * 10)
    master_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Playback preference",
            source_url="https://example.com/test.opus",
            status="ready",
        ))
        db.add_all([
            AudioAssetModel(
                id=master_id,
                session_id=session_id,
                kind="master",
                status="ready",
                path=str(master_path),
                container="matroska",
                codec="opus",
                provenance={"operation": "remux", "audio_transcoded": False},
            ),
            AudioAssetModel(
                id=str(uuid.uuid4()),
                session_id=session_id,
                kind="playback",
                status="ready",
                path=str(playback_path),
                container="mov",
                codec="aac",
                derived_from_id=master_id,
                provenance={"operation": "transcode", "audio_transcoded": True},
            ),
            AudioAssetModel(
                id=str(uuid.uuid4()),
                session_id=session_id,
                kind="inference",
                status="ready",
                path=str(inference_path),
                container="wav",
                codec="pcm_s16le",
                derived_from_id=master_id,
            ),
        ])
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        audio = await ac.get(f"/api/sessions/{session_id}/audio")
        assets = await ac.get(f"/api/sessions/{session_id}/audio-assets")

    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/mp4")
    assert audio.content == b"browser-playback"
    payload = {item["kind"]: item for item in assets.json()}
    assert payload["master"]["provenance"]["audio_transcoded"] is False
    assert payload["playback"]["derived_from_id"] == master_id

@pytest.mark.asyncio
async def test_purge_active_session_guard():
    await init_db()
    session_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Active Session Guard",
            source_url="https://example.com/live.mp3",
            status="live"
        )
        db.add(session)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Purge while live should return 409 Conflict
        purge_res = await ac.delete(f"/api/sessions/{session_id}/purge")
        assert purge_res.status_code == 409
