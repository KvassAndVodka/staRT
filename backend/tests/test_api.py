"""
Integration Tests for FastAPI Endpoints
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.adapters.storage.database import init_db, AsyncSessionLocal
from app.domain.models import SessionModel, InferenceWindowModel, AudioAssetModel
from sqlalchemy import select, update, func

@pytest.mark.asyncio
async def test_health_endpoint():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "default_device" in data

@pytest.mark.asyncio
async def test_session_lifecycle_and_trash():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Create a session
        # This test owns the HTTP lifecycle only. Starting a real recorder here
        # would leak a background task into the next test and race its schema reset.
        with patch(
            "app.api.routes_sessions.coordinator.start_job",
            new_callable=AsyncMock,
        ):
            create_res = await ac.post("/api/sessions", json={
                "url": "https://example.com/test_audio.mp3",
                "language_mode": "auto-mixed"
            })
        assert create_res.status_code == 201
        session_data = create_res.json()
        session_id = session_data["id"]
        assert session_data["source_url"] == "https://example.com/test_audio.mp3"

        async with AsyncSessionLocal() as db:
            db.add(AudioAssetModel(
                id="cascade-asset",
                session_id=session_id,
                kind="master",
                status="failed",
                path="unused",
            ))
            await db.commit()

        # 2. Get session detail
        get_res = await ac.get(f"/api/sessions/{session_id}")
        assert get_res.status_code == 200
        detail = get_res.json()
        assert detail["id"] == session_id
        assert "audio_assets" in detail
        assert detail["event_sequence"] == 0
        assert detail["event_replay_floor"] == 1

        # Mark session as finalized/ready so it is not actively running during purge
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(status="ready")
            )
            await db.commit()

        # 3. Soft-delete to Trash
        trash_res = await ac.delete(f"/api/sessions/{session_id}")
        assert trash_res.status_code == 200
        assert trash_res.json()["status"] == "trashed"

        # 4. Check Trash list
        trash_list_res = await ac.get("/api/sessions/trash")
        assert trash_list_res.status_code == 200
        trashed = trash_list_res.json()
        assert any(s["id"] == session_id for s in trashed)

        # 5. Restore session
        restore_res = await ac.post(f"/api/sessions/{session_id}/restore")
        assert restore_res.status_code == 200
        assert restore_res.json()["status"] == "restored"

        # 6. Purge session permanently (now permitted because session is ready, not live)
        purge_res = await ac.delete(f"/api/sessions/{session_id}/purge")
        assert purge_res.status_code == 200
        assert purge_res.json()["status"] == "purged"

        # 7. Verify session is gone
        gone_res = await ac.get(f"/api/sessions/{session_id}")
        assert gone_res.status_code == 404
        async with AsyncSessionLocal() as db:
            asset_count = (await db.execute(
                select(func.count(AudioAssetModel.id))
                .where(AudioAssetModel.session_id == session_id)
            )).scalar()
            assert asset_count == 0


@pytest.mark.asyncio
async def test_live_detail_falls_back_to_latest_durable_reconciler_snapshot():
    session_id = "snapshot-session"
    snapshot = {
        "committed_words": [{
            "id": "word-1",
            "start_ms": 10,
            "end_ms": 400,
            "text": "durable",
            "speaker_id": None,
            "stability": "committed",
            "confidence": 0.9,
            "language": "en",
        }],
        "provisional_words": [],
        "committed_frontier_ms": 400,
    }
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Snapshot",
            source_url="https://example.com/live",
            status="live",
            active_processing_revision="sample-v2",
        ))
        db.add(InferenceWindowModel(
            id="snapshot-window",
            session_id=session_id,
            stream_epoch=0,
            ordinal=0,
            model_profile_revision="sample-v2",
            target_start_sample=0,
            target_end_sample=16000,
            context_start_sample=0,
            context_end_sample=16000,
            sample_rate_hz=16000,
            target_start_ms=0,
            target_end_ms=1000,
            context_start_ms=0,
            context_end_ms=1000,
            status="succeeded",
            committed_attempt_id="attempt-1",
            input_manifest=[{"fragment_id": "fragment-1"}],
            raw_hypotheses=[],
            reconciler_snapshot=snapshot,
        ))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["turns"][0]["text"] == "durable"
    assert detail["turns"][0]["words"][0]["id"] == "word-1"
