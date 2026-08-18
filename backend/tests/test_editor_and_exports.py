"""
Integration Tests for Turn Editing, Consent Inheritance, and Multi-Format Exports
"""
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.adapters.storage.database import init_db, AsyncSessionLocal
from app.domain.models import (
    SessionModel,
    SpeakerModel,
    TimelineGapModel,
    TranscriptTurnModel,
    WordModel,
)

@pytest.mark.asyncio
async def test_turn_edit_reflects_in_all_export_formats():
    await init_db()
    session_id = str(uuid.uuid4())
    speaker_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    word1_id = str(uuid.uuid4())
    word2_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Export Test Session",
            source_url="https://example.com/stream.mp3",
            status="ready",
            training_consent="excluded"
        )
        db.add(session)
        speaker = SpeakerModel(
            id=speaker_id,
            session_id=session_id,
            machine_label="SPEAKER_00",
            display_name="Speaker 1",
            color="#4f46e5"
        )
        db.add(speaker)
        w1 = WordModel(
            id=word1_id,
            session_id=session_id,
            speaker_id=speaker_id,
            start_ms=0,
            end_ms=1000,
            machine_text="hello",
            stability="finalized"
        )
        w2 = WordModel(
            id=word2_id,
            session_id=session_id,
            speaker_id=speaker_id,
            start_ms=1000,
            end_ms=2000,
            machine_text="world",
            stability="finalized"
        )
        db.add_all([w1, w2])
        turn = TranscriptTurnModel(
            id=turn_id,
            session_id=session_id,
            speaker_id=speaker_id,
            start_ms=0,
            end_ms=2000,
            break_reason="speaker_change"
        )
        db.add(turn)
        gap_start = datetime.now(timezone.utc)
        db.add(TimelineGapModel(
            id=str(uuid.uuid4()),
            session_id=session_id,
            source_start_ms=2000,
            source_end_ms=3000,
            wall_started_at=gap_start,
            wall_ended_at=gap_start + timedelta(seconds=1),
            reason="network",
            recoverable=False,
            recovered=False,
            details={"previous_stream_epoch": 0, "next_stream_epoch": 1},
        ))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Edit the turn text
        edit_res = await ac.patch(f"/api/turns/{turn_id}", json={
            "text": "kumusta Pilipinas"
        })
        assert edit_res.status_code == 200

        # 2. Test TXT export (edited vs machine)
        txt_res = await ac.get(f"/api/sessions/{session_id}/export?format=txt&revision=edited")
        assert txt_res.status_code == 200
        assert "kumusta Pilipinas" in txt_res.text
        assert "hello world" not in txt_res.text
        assert "[audio unavailable during stream interruption]" in txt_res.text

        txt_mach = await ac.get(f"/api/sessions/{session_id}/export?format=txt&revision=machine")
        assert txt_mach.status_code == 200
        assert "hello world" in txt_mach.text

        # 3. Test Markdown export
        md_res = await ac.get(f"/api/sessions/{session_id}/export?format=md&revision=edited")
        assert md_res.status_code == 200
        assert "kumusta Pilipinas" in md_res.text

        # 4. Test SRT export
        srt_res = await ac.get(f"/api/sessions/{session_id}/export?format=srt&revision=edited")
        assert srt_res.status_code == 200
        assert "kumusta Pilipinas" in srt_res.text

        # 5. Test WebVTT export
        vtt_res = await ac.get(f"/api/sessions/{session_id}/export?format=vtt&revision=edited")
        assert vtt_res.status_code == 200
        assert "kumusta Pilipinas" in vtt_res.text

        # 6. Test JSON export
        json_res = await ac.get(f"/api/sessions/{session_id}/export?format=json&revision=edited")
        assert json_res.status_code == 200
        json_data = json_res.json()
        assert json_data["turns"][0]["text"] == "kumusta Pilipinas"
        assert json_data["turns"][0]["edited_text"] == "kumusta Pilipinas"
        assert json_data["turns"][0]["machine_text"] == "hello world"
        assert json_data["session"]["timeline_gaps"][0]["source_start_ms"] == 2000
        assert json_data["session"]["timeline_gaps"][0]["source_end_ms"] == 3000
