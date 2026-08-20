"""Final diarization attribution, overlap truth, and identity reconciliation tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import asc, select

from app.adapters.storage.database import AsyncSessionLocal
from app.main import app
from app.application.job_coordinator import JobCoordinator
from app.application.speaker_pipeline import FinalSpeakerPipeline, FinalSpeakerResult
from app.domain.models import (
    AudioAssetModel,
    AudioFragmentModel,
    OverlapRegionModel,
    SessionModel,
    SpeakerActivityModel,
    SpeakerModel,
    TranscriptTurnModel,
    WordModel,
)
from app.ports.diarization import DiarizationError, DiarizationSegment


class FakeDiarizationEngine:
    def __init__(self, segments: list[DiarizationSegment]) -> None:
        self.segments = segments
        self.calls: list[tuple[str, int, str]] = []
        self.close_count = 0

    async def diarize(self, audio_path, *, duration_ms: int, model_id: str):
        self.calls.append((str(audio_path), duration_ms, model_id))
        return self.segments

    async def close(self) -> None:
        self.close_count += 1


async def _seed_timeline() -> tuple[str, str, str]:
    session_id = "speaker-pipeline"
    host_id = "speaker-host"
    guest_id = "speaker-guest"
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Speakers",
            source_url="https://example.com/audio",
            status="finalizing",
            duration_ms=5000,
        ))
        db.add_all([
            SpeakerModel(
                id=host_id,
                session_id=session_id,
                machine_label="LIVE_00",
                display_name="Host",
                color="#111111",
                sort_order=0,
            ),
            SpeakerModel(
                id=guest_id,
                session_id=session_id,
                machine_label="LIVE_01",
                display_name="Guest",
                color="#222222",
                sort_order=1,
            ),
            SpeakerModel(
                id="speaker-stale",
                session_id=session_id,
                machine_label="LIVE_STALE",
                display_name="Stale",
                color="#333333",
                sort_order=2,
            ),
        ])
        await db.flush()
        db.add_all([
            SpeakerActivityModel(
                id="old-host",
                session_id=session_id,
                speaker_id=host_id,
                start_ms=0,
                end_ms=2500,
                stability="provisional",
            ),
            SpeakerActivityModel(
                id="old-guest",
                session_id=session_id,
                speaker_id=guest_id,
                start_ms=2500,
                end_ms=5000,
                stability="provisional",
            ),
            WordModel(
                id="word-host",
                session_id=session_id,
                start_ms=500,
                end_ms=900,
                machine_text="hello",
                speaker_id=host_id,
            ),
            WordModel(
                id="word-overlap",
                session_id=session_id,
                start_ms=2200,
                end_ms=2300,
                machine_text="both",
                speaker_id=host_id,
            ),
            WordModel(
                id="word-guest",
                session_id=session_id,
                start_ms=4000,
                end_ms=4400,
                machine_text="goodbye",
                speaker_id=host_id,
            ),
        ])
        await db.commit()
    return session_id, host_id, guest_id


@pytest.mark.asyncio
async def test_final_pipeline_preserves_names_and_marks_overlap_words_unresolved():
    session_id, host_id, guest_id = await _seed_timeline()
    segments = [
        DiarizationSegment("FINAL_A", 0, 3000, 0.92),
        DiarizationSegment("FINAL_B", 2000, 5000, 0.88),
    ]
    pipeline = FinalSpeakerPipeline()

    async with AsyncSessionLocal() as db:
        result = await pipeline.apply(db, session_id, segments, duration_ms=5000)
        await db.commit()
        assert result.speaker_count == 2
        assert result.activity_count == 4
        assert result.overlap_count == 1
        assert result.unresolved_word_count == 1

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/sessions/{session_id}")
        assert response.status_code == 200
        snapshot = response.json()
        assert {speaker["display_name"] for speaker in snapshot["speakers"]} == {
            "Host",
            "Guest",
        }
        assert len(snapshot["speaker_activities"]) == 4
        assert snapshot["overlap_regions"][0]["resolution_status"] == "mixed_only"

    async with AsyncSessionLocal() as db:
        speakers = (await db.execute(
            select(SpeakerModel)
            .where(SpeakerModel.session_id == session_id)
            .order_by(asc(SpeakerModel.sort_order))
        )).scalars().all()
        activities = (await db.execute(
            select(SpeakerActivityModel)
            .where(SpeakerActivityModel.session_id == session_id)
            .order_by(asc(SpeakerActivityModel.start_ms), asc(SpeakerActivityModel.speaker_id))
        )).scalars().all()
        overlap = (await db.execute(
            select(OverlapRegionModel).where(OverlapRegionModel.session_id == session_id)
        )).scalars().one()
        words = (await db.execute(
            select(WordModel)
            .where(WordModel.session_id == session_id)
            .order_by(asc(WordModel.start_ms))
        )).scalars().all()
        turns = (await db.execute(
            select(TranscriptTurnModel)
            .where(TranscriptTurnModel.session_id == session_id)
            .order_by(asc(TranscriptTurnModel.start_ms))
        )).scalars().all()

        assert [(speaker.id, speaker.machine_label, speaker.display_name) for speaker in speakers] == [
            (host_id, "FINAL_A", "Host"),
            (guest_id, "FINAL_B", "Guest"),
        ]
        assert [(item.start_ms, item.end_ms) for item in activities] == [
            (0, 2000),
            (2000, 3000),
            (2000, 3000),
            (3000, 5000),
        ]
        assert overlap.start_ms == 2000
        assert overlap.end_ms == 3000
        assert overlap.resolution_status == "mixed_only"
        assert overlap.hypotheses == []
        assert set(overlap.speaker_activity_ids) == {
            item.id for item in activities if item.overlap_group == overlap.id
        }
        assert [word.speaker_id for word in words] == [host_id, None, guest_id]
        assert [turn.speaker_id for turn in turns] == [host_id, None, guest_id]

    async with AsyncSessionLocal() as db:
        second = await pipeline.apply(db, session_id, segments, duration_ms=5000)
        await db.commit()
        assert second.activity_count == 4
        assert second.overlap_count == 1
        speaker_count = (await db.execute(
            select(SpeakerModel).where(SpeakerModel.session_id == session_id)
        )).scalars().all()
        assert len(speaker_count) == 2


@pytest.mark.asyncio
async def test_same_speaker_intervals_merge_without_false_overlap():
    session_id = "merged-speaker"
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Merged",
            source_url="https://example.com/audio",
            status="finalizing",
            duration_ms=2000,
        ))
        await db.commit()
        result = await FinalSpeakerPipeline().apply(db, session_id, [
            DiarizationSegment("SPEAKER_00", 0, 1000, 0.9),
            DiarizationSegment("SPEAKER_00", 500, 1500, 0.8),
        ], duration_ms=2000)
        await db.commit()
        assert result.activity_count == 1
        assert result.overlap_count == 0


@pytest.mark.asyncio
async def test_cluster_identity_uses_global_maximum_overlap_assignment():
    session_id = "maximum-assignment"
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Assignment",
            source_url="https://example.com/audio",
            status="finalizing",
            duration_ms=30,
        ))
        db.add_all([
            SpeakerModel(
                id="speaker-x",
                session_id=session_id,
                machine_label="OLD_X",
                display_name="X name",
                sort_order=0,
            ),
            SpeakerModel(
                id="speaker-y",
                session_id=session_id,
                machine_label="OLD_Y",
                display_name="Y name",
                sort_order=1,
            ),
        ])
        await db.flush()
        db.add_all([
            SpeakerActivityModel(
                id="activity-x",
                session_id=session_id,
                speaker_id="speaker-x",
                start_ms=0,
                end_ms=18,
            ),
            SpeakerActivityModel(
                id="activity-y",
                session_id=session_id,
                speaker_id="speaker-y",
                start_ms=20,
                end_ms=29,
            ),
        ])
        await db.commit()

        await FinalSpeakerPipeline().apply(db, session_id, [
            DiarizationSegment("FINAL_A", 0, 10),
            DiarizationSegment("FINAL_A", 20, 29),
            DiarizationSegment("FINAL_B", 10, 18),
        ], duration_ms=30)
        await db.commit()

        speakers = (await db.execute(
            select(SpeakerModel)
            .where(SpeakerModel.session_id == session_id)
            .order_by(asc(SpeakerModel.id))
        )).scalars().all()
        assert [(speaker.id, speaker.machine_label) for speaker in speakers] == [
            ("speaker-x", "FINAL_B"),
            ("speaker-y", "FINAL_A"),
        ]


@pytest.mark.asyncio
async def test_invalid_diarization_does_not_replace_existing_truth():
    session_id, _host_id, _guest_id = await _seed_timeline()
    async with AsyncSessionLocal() as db:
        with pytest.raises(DiarizationError, match="exceeds the session duration"):
            await FinalSpeakerPipeline().apply(db, session_id, [
                DiarizationSegment("FINAL_A", 0, 5001, 0.9),
            ], duration_ms=5000)
        activities = (await db.execute(
            select(SpeakerActivityModel)
            .where(SpeakerActivityModel.session_id == session_id)
        )).scalars().all()
        assert {activity.id for activity in activities} == {"old-host", "old-guest"}


@pytest.mark.asyncio
async def test_coordinator_runs_adapter_on_ready_inference_audio_and_releases_it(tmp_path):
    session_id = "adapter-boundary"
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Adapter",
            source_url="https://example.com/audio",
            status="finalizing",
        ))
        await db.flush()
        db.add(AudioAssetModel(
            id="inference-asset",
            session_id=session_id,
            kind="inference",
            status="ready",
            path=str(audio_path),
        ))
        db.add_all([
            AudioFragmentModel(
                id="fragment-epoch-0",
                session_id=session_id,
                sequence=0,
                stream_epoch=0,
                sample_start=0,
                sample_end=16,
                sample_count=16,
                sample_rate_hz=16,
                bytes_per_sample=2,
                source_start_ms=0,
                source_end_ms=1000,
                path=str(tmp_path / "fragment-0.raw"),
                sha256="a" * 64,
                status="durable",
            ),
            AudioFragmentModel(
                id="fragment-epoch-1",
                session_id=session_id,
                sequence=0,
                stream_epoch=1,
                sample_start=0,
                sample_end=16,
                sample_count=16,
                sample_rate_hz=16,
                bytes_per_sample=2,
                source_start_ms=2000,
                source_end_ms=3000,
                path=str(tmp_path / "fragment-1.raw"),
                sha256="b" * 64,
                status="durable",
            ),
        ])
        await db.commit()

    engine = FakeDiarizationEngine([
        DiarizationSegment("SPEAKER_00", 500, 1500, 0.9),
    ])
    coordinator = JobCoordinator(diarization_engine=engine)
    actual = await coordinator._run_final_diarization(session_id, 3000)

    assert actual == [
        DiarizationSegment("SPEAKER_00", 500, 1000, 0.9),
        DiarizationSegment("SPEAKER_00", 2000, 2500, 0.9),
    ]
    assert engine.calls == [(str(audio_path), 2000, "pyannote-community-1")]
    assert engine.close_count == 1


@pytest.mark.asyncio
async def test_finalization_applies_speaker_truth_before_ready_event():
    session_id = "finalization-wiring"
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Final",
            source_url="https://example.com/audio",
            status="live",
        ))
        await db.commit()

    publisher = SimpleNamespace(broadcast_event=AsyncMock())
    pipeline = SimpleNamespace(apply=AsyncMock(return_value=FinalSpeakerResult(
        speaker_count=2,
        activity_count=4,
        overlap_count=1,
        unresolved_word_count=1,
    )))
    coordinator = JobCoordinator(event_publisher=publisher, speaker_pipeline=pipeline)
    coordinator._finalize_assets = AsyncMock()
    coordinator._run_final_diarization = AsyncMock(return_value=[
        DiarizationSegment("SPEAKER_00", 0, 1000, 0.9),
    ])
    coordinator._persist_final_transcript = AsyncMock()
    coordinator.validate_ready_invariants = AsyncMock()
    reconciler = SimpleNamespace(committed_frontier_ms=1000)

    await coordinator._finalize_session(
        session_id,
        reconciler,
        "speaker-default",
        SimpleNamespace(),
        16000,
        16000,
        "sample-v2",
        recovery=False,
        source_duration_ms=1000,
    )

    pipeline.apply.assert_awaited_once()
    event_types = [call.args[1] for call in publisher.broadcast_event.await_args_list]
    assert event_types == ["speaker.upsert", "overlap.upsert", "session.ready"]
    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        assert session.status == "ready"
