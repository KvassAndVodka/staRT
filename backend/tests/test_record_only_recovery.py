"""Deterministic record-only, recovery, lease, and readiness contracts."""
from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import numpy as np
import pytest
from sqlalchemy import select, asc, delete, func

from app.adapters.storage.database import AsyncSessionLocal
from app.domain.models import (
    SessionModel,
    InferenceWindowModel,
    InferenceAttemptModel,
    AudioAssetModel,
    AudioFragmentModel,
    TimelineGapModel,
    OutboxEventModel,
    WordModel,
)
from app.application.job_coordinator import (
    JobCoordinator,
    LagPolicy,
    ReadinessValidationError,
    LostLeaseError,
)
from app.application.continuity import WordContinuityReconciler
from app.adapters.ingestion.stream_capture import (
    CapturedFragment,
    ProbedMediaInfo,
    ResolvedSource,
    SourceReconnecting,
    StreamDiscontinuity,
)
from app.adapters.asr.faster_whisper_engine import HypothesisWord
from app.config import settings


async def _wait_for_mode(session_id: str, expected: str) -> None:
    for _ in range(100):
        async with AsyncSessionLocal() as db:
            session = await db.get(SessionModel, session_id)
            if session and session.processing_mode == expected:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Session did not enter {expected}")


def test_zero_duration_pts_reset_is_a_valid_epoch_boundary():
    boundary = datetime.now(timezone.utc)
    gap = TimelineGapModel(
        id="pts-reset-gap",
        session_id="session",
        source_start_ms=1000,
        source_end_ms=1000,
        wall_started_at=boundary,
        wall_ended_at=boundary,
        reason="source_pts_reset",
        details={"previous_stream_epoch": 0, "next_stream_epoch": 1},
    )

    JobCoordinator._validate_epoch_gap_chain(
        {0: (0, 1000), 1: (1000, 2000)},
        [gap],
        ReadinessValidationError,
    )


@pytest.mark.asyncio
async def test_record_only_pauses_claims_while_capture_active():
    session_id = str(uuid.uuid4())
    test_coordinator = JobCoordinator(
        lag_policy=LagPolicy(catching_up_threshold_items=1, record_only_threshold_items=2)
    )
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Record Only Active Test",
            source_url="https://example.com/live_test.mp3",
            status="queued",
        ))
        await db.commit()

    frag_dir = settings.SESSIONS_DIR / session_id / "fragments"
    audio_dir = settings.SESSIONS_DIR / session_id / "audio"
    frag_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "master.mka").write_bytes(b"MASTER_DUMMY_DATA" * 10)
    (audio_dir / "playback.m4a").write_bytes(b"PLAYBACK_DUMMY_DATA" * 10)
    (audio_dir / "inference.wav").write_bytes(b"RIFF" + b"\x00" * 100)

    fragments = []
    for sequence in range(5):
        start = sequence * 32000
        end = start + 32000
        path = frag_dir / f"frag_{sequence:06d}_{start}_{end}.raw"
        data = np.full(32000, (sequence + 1) * 1000, dtype=np.int16).tobytes()
        path.write_bytes(data)
        fragments.append((path, hashlib.sha256(data).hexdigest()))

    producer_gate = asyncio.Event()
    producer_loaded = asyncio.Event()

    async def mock_stream_fragments(*args, **kwargs):
        for sequence, (path, checksum) in enumerate(fragments):
            start = sequence * 32000
            end = start + 32000
            yield (
                sequence,
                start,
                end,
                32000,
                sequence * 2000,
                (sequence + 1) * 2000,
                np.zeros(32000, dtype=np.float32),
                path,
                checksum,
            )
        producer_loaded.set()
        await producer_gate.wait()

    inference_entered = threading.Event()
    release_inference = threading.Event()
    submitted: list[tuple[int, int, tuple[int, ...]]] = []

    def fake_transcribe(audio_chunk, window_start_ms, language_mode):
        values = tuple(int(round(value * 32768)) for value in np.unique(audio_chunk))
        submitted.append((window_start_ms, len(audio_chunk), values))
        inference_entered.set()
        if not release_inference.is_set():
            release_inference.wait(timeout=5.0)
        return [HypothesisWord(
            start_ms=window_start_ms,
            end_ms=window_start_ms + 500,
            text=f"word_{window_start_ms}",
        )]

    engine = MagicMock()
    engine.actual_device = "cuda"
    engine.actual_compute_type = "int8_float16"
    engine.transcribe_window.side_effect = fake_transcribe

    with patch("app.application.job_coordinator.StreamIngestionAdapter.resolve_source", new_callable=AsyncMock) as resolve:
        resolve.return_value = ResolvedSource(
            title="Record Only Source",
            media_url="https://example.com/live_test.mp3",
            is_live=True,
            duration_sec=10.0,
            http_headers={},
        )
        with patch("app.application.job_coordinator.StreamIngestionAdapter.probe_media_file", new_callable=AsyncMock) as probe:
            probe.return_value = ProbedMediaInfo("m4a", "aac", 2, 44100, 10000, 2048)
            with patch.object(test_coordinator, "get_asr_engine", new_callable=AsyncMock) as get_engine:
                get_engine.return_value = engine
                with patch(
                    "app.application.job_coordinator.StreamIngestionAdapter.stream_pcm_fragments",
                    mock_stream_fragments,
                ):
                    pipeline = asyncio.create_task(test_coordinator._run_job(session_id))
                    assert await asyncio.to_thread(inference_entered.wait, 5.0)
                    await producer_loaded.wait()
                    release_inference.set()
                    await _wait_for_mode(session_id, "record_only")

                    calls_at_record_only = len(submitted)
                    await asyncio.sleep(0.1)
                    assert len(submitted) == calls_at_record_only

                    producer_gate.set()
                    await pipeline

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        assert session.status == "ready", session.error_code
        result = await db.execute(
            select(InferenceWindowModel)
            .where(InferenceWindowModel.session_id == session_id)
            .order_by(asc(InferenceWindowModel.ordinal))
        )
        windows = result.scalars().all()
        assert windows
        assert all(window.status == "succeeded" for window in windows)

        assert windows[-1].target_end_sample == 160000
        assert all(window.attempt_count == 1 for window in windows)
        asset_result = await db.execute(
            select(AudioAssetModel).where(AudioAssetModel.session_id == session_id)
        )
        assets = {asset.kind: asset for asset in asset_result.scalars().all()}
        assert set(assets) == {"master", "playback", "inference"}
        assert assets["master"].provenance["audio_transcoded"] is False
        assert assets["playback"].derived_from_id == assets["master"].id
        assert assets["playback"].provenance["target_codec"] == "aac"
        assert assets["inference"].derived_from_id == assets["master"].id

    assert submitted[0][2] == (1000,)
    assert submitted[-1][2] == (1000, 2000, 3000, 4000, 5000)
    await test_coordinator.shutdown()


@pytest.mark.asyncio
async def test_startup_recovery_drains_existing_work_without_recapture():
    session_id = str(uuid.uuid4())
    fragment_dir = settings.SESSIONS_DIR / session_id / "fragments"
    audio_dir = settings.SESSIONS_DIR / session_id / "audio"
    fragment_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    raw = np.full(32000, 700, dtype=np.int16).tobytes()
    fragment_path = fragment_dir / "frag_000000_0_32000.raw"
    fragment_path.write_bytes(raw)
    master_path = audio_dir / "master.m4a"
    inference_path = audio_dir / "inference.wav"
    master_path.write_bytes(b"recoverable-master")
    inference_path.write_bytes(b"stale-wave")

    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    succeeded_id = str(uuid.uuid4())
    pending_id = str(uuid.uuid4())
    old_attempt_id = str(uuid.uuid4())
    snapshot = {"committed_words": [], "provisional_words": [], "committed_frontier_ms": 0}
    manifest = [{"fragment_id": "f0", "sequence": 0, "sha256": hashlib.sha256(raw).hexdigest()}]

    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Recovery",
            source_url="https://example.com/recovery.mp3",
            status="live",
            active_processing_revision="sample-v2",
        ))
        db.add(AudioFragmentModel(
            id="f0",
            session_id=session_id,
            sequence=0,
            stream_epoch=0,
            sample_start=0,
            sample_end=32000,
            sample_count=32000,
            sample_rate_hz=16000,
            bytes_per_sample=2,
            source_start_ms=0,
            source_end_ms=2000,
            path=str(fragment_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            status="durable",
        ))
        db.add_all([
            AudioAssetModel(
                id=str(uuid.uuid4()), session_id=session_id, kind="master",
                status="writing", path=str(master_path),
            ),
            AudioAssetModel(
                id=str(uuid.uuid4()), session_id=session_id, kind="inference",
                status="writing", path=str(inference_path),
            ),
        ])
        db.add(InferenceWindowModel(
            id=succeeded_id,
            session_id=session_id,
            stream_epoch=0,
            ordinal=0,
            model_profile_revision="sample-v2",
            target_start_sample=0,
            target_end_sample=24000,
            context_start_sample=0,
            context_end_sample=24000,
            sample_rate_hz=16000,
            target_start_ms=0,
            target_end_ms=1500,
            context_start_ms=0,
            context_end_ms=1500,
            status="succeeded",
            committed_attempt_id="committed-attempt",
            input_manifest=manifest,
            raw_hypotheses=[],
            reconciler_snapshot=snapshot,
        ))
        db.add(InferenceWindowModel(
            id=pending_id,
            session_id=session_id,
            stream_epoch=0,
            ordinal=1,
            model_profile_revision="sample-v2",
            target_start_sample=24000,
            target_end_sample=32000,
            context_start_sample=0,
            context_end_sample=32000,
            sample_rate_hz=16000,
            target_start_ms=1500,
            target_end_ms=2000,
            context_start_ms=0,
            context_end_ms=2000,
            status="running",
            attempt_count=1,
            lease_owner="dead-worker",
            lease_expires_at=expired,
            active_attempt_id=old_attempt_id,
        ))
        db.add(InferenceAttemptModel(
            id=old_attempt_id,
            window_id=pending_id,
            attempt_number=1,
            worker_id="dead-worker",
            status="running",
        ))
        await db.commit()

    engine = MagicMock()
    engine.actual_device = "cpu"
    engine.actual_compute_type = "int8"
    engine.transcribe_window.return_value = [
        HypothesisWord(start_ms=1500, end_ms=1900, text="recovered")
    ]
    recovered = JobCoordinator()

    async def recapture_must_not_run(*args, **kwargs):
        raise AssertionError("Recovery attempted to recapture the source")
        yield  # pragma: no cover

    with patch.object(recovered, "get_asr_engine", new_callable=AsyncMock) as get_engine:
        get_engine.return_value = engine
        with patch("app.application.job_coordinator.StreamIngestionAdapter.probe_media_file", new_callable=AsyncMock) as probe:
            probe.return_value = ProbedMediaInfo("m4a", "aac", 2, 44100, 2000, 1024)
            with patch(
                "app.application.job_coordinator.StreamIngestionAdapter.stream_pcm_fragments",
                recapture_must_not_run,
            ):
                await recovered.startup_recovery()
                recovery_task = recovered.active_task
                assert recovery_task is not None
                await recovery_task

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        assert session.status == "ready", session.error_code
        first = await db.get(InferenceWindowModel, succeeded_id)
        second = await db.get(InferenceWindowModel, pending_id)
        assert first.attempt_count == 0
        assert second.status == "succeeded"
        assert second.attempt_count == 2
        assert second.lease_owner is None
        old_attempt = await db.get(InferenceAttemptModel, old_attempt_id)
        assert old_attempt.status == "superseded"

    assert engine.transcribe_window.call_count == 1
    await recovered.shutdown()


@pytest.mark.asyncio
async def test_live_discontinuity_creates_separate_epochs_and_explicit_gap():
    session_id = str(uuid.uuid4())
    event_publisher = MagicMock()
    event_publisher.broadcast_event = AsyncMock()
    test_coordinator = JobCoordinator(event_publisher=event_publisher)
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Epoch capture",
            source_url="https://example.com/live-epochs.m3u8",
            status="queued",
        ))
        await db.commit()

    fragment_dir = settings.SESSIONS_DIR / session_id / "fragments"
    audio_dir = settings.SESSIONS_DIR / session_id / "audio"
    fragment_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "master.mka").write_bytes(b"epoch-master")
    (audio_dir / "playback.m4a").write_bytes(b"epoch-playback")
    (audio_dir / "inference.wav").write_bytes(b"RIFF" + b"\x00" * 100)
    first_bytes = np.full(16000, 111, dtype=np.int16).tobytes()
    second_bytes = np.full(16000, 222, dtype=np.int16).tobytes()
    first_path = fragment_dir / "epoch_0000_frag_000000_0_16000.raw"
    second_path = fragment_dir / "epoch_0001_frag_000000_0_16000.raw"
    first_path.write_bytes(first_bytes)
    second_path.write_bytes(second_bytes)
    wall_start = datetime.now(timezone.utc)

    async def epoch_stream(*args, **kwargs):
        yield CapturedFragment(
            sequence=0,
            stream_epoch=0,
            sample_start=0,
            sample_end=16000,
            sample_count=16000,
            source_start_ms=0,
            source_end_ms=1000,
            wall_started_at=wall_start,
            wall_ended_at=wall_start + timedelta(seconds=1),
            source_pts_start=None,
            source_pts_end=None,
            audio=np.zeros(16000, dtype=np.float32),
            path=first_path,
            sha256=hashlib.sha256(first_bytes).hexdigest(),
        )
        yield SourceReconnecting(
            stream_epoch=0,
            wall_started_at=wall_start + timedelta(seconds=1),
        )
        yield StreamDiscontinuity(
            previous_stream_epoch=0,
            next_stream_epoch=1,
            source_start_ms=1000,
            source_end_ms=4000,
            wall_started_at=wall_start + timedelta(seconds=1),
            wall_ended_at=wall_start + timedelta(seconds=4),
        )
        yield CapturedFragment(
            sequence=0,
            stream_epoch=1,
            sample_start=0,
            sample_end=16000,
            sample_count=16000,
            source_start_ms=4000,
            source_end_ms=5000,
            wall_started_at=wall_start + timedelta(seconds=4),
            wall_ended_at=wall_start + timedelta(seconds=5),
            source_pts_start=None,
            source_pts_end=None,
            audio=np.zeros(16000, dtype=np.float32),
            path=second_path,
            sha256=hashlib.sha256(second_bytes).hexdigest(),
        )

    engine = MagicMock()
    engine.actual_device = "cpu"
    engine.actual_compute_type = "int8"
    engine.transcribe_window.side_effect = lambda audio, start_ms, language: [
        HypothesisWord(
            start_ms=start_ms,
            end_ms=start_ms + 500,
            text=f"epoch-{start_ms}",
        )
    ]

    with patch("app.application.job_coordinator.StreamIngestionAdapter.resolve_source", new_callable=AsyncMock) as resolve:
        resolve.return_value = ResolvedSource(
            title="Epoch stream",
            media_url="https://example.com/live-epochs.m3u8",
            is_live=True,
            duration_sec=None,
            http_headers={},
        )
        with patch("app.application.job_coordinator.StreamIngestionAdapter.probe_media_file", new_callable=AsyncMock) as probe:
            probe.return_value = ProbedMediaInfo("m4a", "aac", 2, 44100, 5000, 1024)
            with patch.object(test_coordinator, "get_asr_engine", new_callable=AsyncMock) as get_engine:
                get_engine.return_value = engine
                with patch(
                    "app.application.job_coordinator.StreamIngestionAdapter.stream_pcm_fragments",
                    epoch_stream,
                ):
                    await test_coordinator._run_job(session_id)

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        assert session.status == "ready", session.error_code
        assert session.duration_ms == 5000
        fragments = (await db.execute(
            select(AudioFragmentModel)
            .where(AudioFragmentModel.session_id == session_id)
            .order_by(asc(AudioFragmentModel.stream_epoch))
        )).scalars().all()
        assert [(item.stream_epoch, item.sequence, item.sample_start, item.sample_end) for item in fragments] == [
            (0, 0, 0, 16000),
            (1, 0, 0, 16000),
        ]
        gap = (await db.execute(
            select(TimelineGapModel).where(TimelineGapModel.session_id == session_id)
        )).scalars().one()
        assert (gap.source_start_ms, gap.source_end_ms) == (1000, 4000)
        assert gap.details == {"previous_stream_epoch": 0, "next_stream_epoch": 1}
        windows = (await db.execute(
            select(InferenceWindowModel)
            .where(InferenceWindowModel.session_id == session_id)
            .order_by(asc(InferenceWindowModel.stream_epoch))
        )).scalars().all()
        assert [
            (item.stream_epoch, item.target_start_sample, item.target_end_sample,
             item.target_start_ms, item.target_end_ms)
            for item in windows
        ] == [
            (0, 0, 16000, 0, 1000),
            (1, 0, 16000, 4000, 5000),
        ]
        words = (await db.execute(
            select(WordModel)
            .where(WordModel.session_id == session_id)
            .order_by(asc(WordModel.start_ms))
        )).scalars().all()
        assert [(word.start_ms, word.machine_text) for word in words] == [
            (0, "epoch-0"),
            (4000, "epoch-4000"),
        ]

    assert [call.args[1] for call in engine.transcribe_window.call_args_list] == [0, 4000]
    event_types = [call.args[1] for call in event_publisher.broadcast_event.call_args_list]
    assert "source.reconnecting" in event_types
    assert "source.reconnected" in event_types
    await test_coordinator.shutdown()


@pytest.mark.asyncio
async def test_terminal_reconnect_outage_is_persisted_and_not_finalized_ready():
    session_id = str(uuid.uuid4())
    test_coordinator = JobCoordinator()
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Terminal outage",
            source_url="https://example.com/outage.m3u8",
            status="queued",
        ))
        await db.commit()

    fragment_dir = settings.SESSIONS_DIR / session_id / "fragments"
    fragment_dir.mkdir(parents=True, exist_ok=True)
    raw = np.zeros(16000, dtype=np.int16).tobytes()
    path = fragment_dir / "epoch_0000_frag_000000_0_16000.raw"
    path.write_bytes(raw)
    reconnect_started = datetime.now(timezone.utc)

    async def interrupted_stream(*args, **kwargs):
        yield (
            0, 0, 16000, 16000, 0, 1000,
            np.zeros(16000, dtype=np.float32),
            path,
            hashlib.sha256(raw).hexdigest(),
        )
        yield SourceReconnecting(
            stream_epoch=0,
            wall_started_at=reconnect_started,
        )

    engine = MagicMock()
    engine.actual_device = "cpu"
    engine.actual_compute_type = "int8"
    engine.transcribe_window.return_value = []
    with patch("app.application.job_coordinator.StreamIngestionAdapter.resolve_source", new_callable=AsyncMock) as resolve:
        resolve.return_value = ResolvedSource(
            title="Terminal outage",
            media_url="https://example.com/outage.m3u8",
            is_live=True,
            duration_sec=None,
            http_headers={},
        )
        with patch.object(test_coordinator, "get_asr_engine", new_callable=AsyncMock) as get_engine:
            get_engine.return_value = engine
            with patch(
                "app.application.job_coordinator.StreamIngestionAdapter.stream_pcm_fragments",
                interrupted_stream,
            ):
                await test_coordinator._run_job(session_id)

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        assert session.status == "failed"
        assert "Source ended before a reconnect completed" in session.error_code
        gap = (await db.execute(
            select(TimelineGapModel).where(TimelineGapModel.session_id == session_id)
        )).scalars().one()
        assert gap.source_start_ms == 1000
        assert gap.source_end_ms is None
        assert gap.wall_ended_at is None
        assert gap.details == {"previous_stream_epoch": 0, "next_stream_epoch": None}

    await test_coordinator.shutdown()


@pytest.mark.asyncio
async def test_restart_recovery_preserves_two_epoch_mapping_without_recapture():
    session_id = str(uuid.uuid4())
    fragment_dir = settings.SESSIONS_DIR / session_id / "fragments"
    audio_dir = settings.SESSIONS_DIR / session_id / "audio"
    fragment_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "master.m4a").write_bytes(b"restart-master")
    (audio_dir / "inference.wav").write_bytes(b"stale")
    wall_start = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Epoch restart",
            source_url="https://example.com/restart.m3u8",
            status="live",
        ))
        for epoch, offset, value in ((0, 0, 10), (1, 4000, 20)):
            raw = np.full(16000, value, dtype=np.int16).tobytes()
            path = fragment_dir / f"epoch_{epoch:04d}_frag_000000_0_16000.raw"
            path.write_bytes(raw)
            db.add(AudioFragmentModel(
                id=f"epoch-fragment-{epoch}",
                session_id=session_id,
                sequence=0,
                stream_epoch=epoch,
                sample_start=0,
                sample_end=16000,
                sample_count=16000,
                sample_rate_hz=16000,
                bytes_per_sample=2,
                source_start_ms=offset,
                source_end_ms=offset + 1000,
                wall_started_at=wall_start + timedelta(seconds=offset / 1000),
                wall_ended_at=wall_start + timedelta(seconds=offset / 1000 + 1),
                path=str(path),
                sha256=hashlib.sha256(raw).hexdigest(),
                status="durable",
            ))
        db.add(TimelineGapModel(
            id="epoch-gap",
            session_id=session_id,
            source_start_ms=1000,
            source_end_ms=4000,
            wall_started_at=wall_start + timedelta(seconds=1),
            wall_ended_at=wall_start + timedelta(seconds=4),
            reason="network",
            recoverable=False,
            recovered=False,
            details={"previous_stream_epoch": 0, "next_stream_epoch": 1},
        ))
        db.add_all([
            AudioAssetModel(
                id=str(uuid.uuid4()), session_id=session_id, kind="master",
                status="writing", path=str(audio_dir / "master.m4a"),
            ),
            AudioAssetModel(
                id=str(uuid.uuid4()), session_id=session_id, kind="inference",
                status="writing", path=str(audio_dir / "inference.wav"),
            ),
        ])
        await db.commit()

    engine = MagicMock()
    engine.actual_device = "cpu"
    engine.actual_compute_type = "int8"
    engine.transcribe_window.side_effect = lambda audio, start_ms, language: [
        HypothesisWord(start_ms=start_ms, end_ms=start_ms + 500, text=f"r-{start_ms}")
    ]
    recovered = JobCoordinator()

    async def recapture_must_not_run(*args, **kwargs):
        raise AssertionError("Epoch recovery attempted to recapture")
        yield  # pragma: no cover

    with patch.object(recovered, "get_asr_engine", new_callable=AsyncMock) as get_engine:
        get_engine.return_value = engine
        with patch("app.application.job_coordinator.StreamIngestionAdapter.probe_media_file", new_callable=AsyncMock) as probe:
            probe.return_value = ProbedMediaInfo("m4a", "aac", 2, 44100, 5000, 1024)
            with patch(
                "app.application.job_coordinator.StreamIngestionAdapter.stream_pcm_fragments",
                recapture_must_not_run,
            ):
                await recovered.startup_recovery()
                assert recovered.active_task is not None
                await recovered.active_task

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        assert session.status == "ready", session.error_code
        assert session.duration_ms == 5000
        windows = (await db.execute(
            select(InferenceWindowModel)
            .where(InferenceWindowModel.session_id == session_id)
            .order_by(asc(InferenceWindowModel.stream_epoch))
        )).scalars().all()
        assert [(window.stream_epoch, window.target_start_ms) for window in windows] == [
            (0, 0),
            (1, 4000),
        ]
        assert all(window.status == "succeeded" for window in windows)

        await db.execute(
            delete(TimelineGapModel).where(TimelineGapModel.session_id == session_id)
        )
        await db.commit()
        with pytest.raises(ReadinessValidationError, match="Expected 1 timeline gaps"):
            await JobCoordinator.validate_ready_invariants(
                db,
                session_id,
                total_duration_ms=5000,
                epoch_frontiers={0: 16000, 1: 16000},
                sample_rate=16000,
                revision="sample-v2",
            )

    assert [call.args[1] for call in engine.transcribe_window.call_args_list] == [0, 4000]
    await recovered.shutdown()


@pytest.mark.asyncio
async def test_readiness_validator_rejects_exact_sample_gap():
    session_id = str(uuid.uuid4())
    fragment_path = settings.SESSIONS_DIR / session_id / "fragments" / "f.raw"
    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    data = np.zeros(32000, dtype=np.int16).tobytes()
    fragment_path.write_bytes(data)

    async with AsyncSessionLocal() as db:
        db.add(SessionModel(id=session_id, title="Gap", source_url="https://example.com/gap", status="finalizing"))
        db.add(AudioFragmentModel(
            id="gap-fragment", session_id=session_id, sequence=0, stream_epoch=0,
            sample_start=0, sample_end=32000, sample_count=32000,
            sample_rate_hz=16000, bytes_per_sample=2, source_start_ms=0, source_end_ms=2000,
            path=str(fragment_path), sha256=hashlib.sha256(data).hexdigest(), status="durable",
        ))
        db.add_all([
            AudioAssetModel(id=str(uuid.uuid4()), session_id=session_id, kind="master", status="ready", path="master"),
            AudioAssetModel(id=str(uuid.uuid4()), session_id=session_id, kind="inference", status="ready", path="inference"),
        ])
        for ordinal, start, end in ((0, 0, 16000), (1, 16001, 32000)):
            db.add(InferenceWindowModel(
                id=str(uuid.uuid4()), session_id=session_id, stream_epoch=0, ordinal=ordinal,
                model_profile_revision="sample-v2", target_start_sample=start, target_end_sample=end,
                context_start_sample=0, context_end_sample=end, sample_rate_hz=16000,
                target_start_ms=int(start / 16), target_end_ms=int(end / 16),
                context_start_ms=0, context_end_ms=int(end / 16), status="succeeded",
                committed_attempt_id=str(uuid.uuid4()), input_manifest=[{"fragment_id": "gap-fragment"}],
                raw_hypotheses=[], reconciler_snapshot={},
            ))
        await db.commit()

    async with AsyncSessionLocal() as db:
        with pytest.raises(ReadinessValidationError, match="gap or overlap"):
            await JobCoordinator.validate_ready_invariants(
                db,
                session_id,
                total_samples=32000,
                sample_rate=16000,
                revision="sample-v2",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("frontier_sample", [1, 319, 320, 321, 23999, 24000, 24001])
async def test_tail_window_reaches_every_exact_sample_frontier(frontier_sample: int):
    session_id = str(uuid.uuid4())
    test_coordinator = JobCoordinator()
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Exact tail",
            source_url="https://example.com/tail.raw",
            status="finalizing",
            active_processing_revision="sample-v2",
        ))
        await db.flush()
        await test_coordinator._add_available_windows(
            db,
            session_id,
            "sample-v2",
            0,
            frontier_sample,
            include_tail=True,
            sample_rate=16000,
        )
        await db.commit()
        windows = (await db.execute(
            select(InferenceWindowModel)
            .where(InferenceWindowModel.session_id == session_id)
            .order_by(asc(InferenceWindowModel.ordinal))
        )).scalars().all()

    assert windows
    assert windows[0].target_start_sample == 0
    assert windows[-1].target_end_sample == frontier_sample
    assert all(window.target_end_sample > window.target_start_sample for window in windows)
    assert all(
        right.target_start_sample == left.target_end_sample
        for left, right in zip(windows, windows[1:])
    )
    await test_coordinator.shutdown()


@pytest.mark.asyncio
async def test_startup_recovery_rejects_missing_fragment_before_model_load():
    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Corrupt recovery",
            source_url="https://example.com/missing.mp3",
            status="live",
        ))
        db.add(AudioFragmentModel(
            id=str(uuid.uuid4()), session_id=session_id, sequence=0, stream_epoch=0,
            sample_start=0, sample_end=16000, sample_count=16000,
            sample_rate_hz=16000, bytes_per_sample=2, source_start_ms=0, source_end_ms=1000,
            path=str(settings.SESSIONS_DIR / session_id / "missing.raw"),
            sha256="0" * 64, status="durable",
        ))
        db.add_all([
            AudioAssetModel(id=str(uuid.uuid4()), session_id=session_id, kind="master", status="writing", path="master"),
            AudioAssetModel(id=str(uuid.uuid4()), session_id=session_id, kind="inference", status="writing", path="inference"),
        ])
        await db.commit()

    recovered = JobCoordinator()
    with patch.object(recovered, "get_asr_engine", new_callable=AsyncMock) as get_engine:
        await recovered.startup_recovery()
        task = recovered.active_task
        if task is not None:
            await task
        get_engine.assert_not_called()

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        assert session.status == "failed"
        assert session.error_code == "server_interrupted_no_verified_audio"
        assets = (await db.execute(
            select(AudioAssetModel).where(AudioAssetModel.session_id == session_id)
        )).scalars().all()
        assert all(asset.status == "failed" for asset in assets)
    await recovered.shutdown()


@pytest.mark.asyncio
async def test_stale_worker_cannot_commit_after_replacement_claim():
    session_id = str(uuid.uuid4())
    fragment_dir = settings.SESSIONS_DIR / session_id / "fragments"
    fragment_dir.mkdir(parents=True, exist_ok=True)
    data = np.full(16000, 321, dtype=np.int16).tobytes()
    path = fragment_dir / "f.raw"
    path.write_bytes(data)

    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Lease fencing",
            source_url="https://example.com/lease.mp3",
            status="finalizing",
        ))
        db.add(AudioFragmentModel(
            id="lease-fragment", session_id=session_id, sequence=0, stream_epoch=0,
            sample_start=0, sample_end=16000, sample_count=16000,
            sample_rate_hz=16000, bytes_per_sample=2, source_start_ms=0, source_end_ms=1000,
            path=str(path), sha256=hashlib.sha256(data).hexdigest(), status="durable",
        ))
        coordinator_for_window = JobCoordinator()
        db.add(coordinator_for_window._new_window(
            session_id, "sample-v2", 0, 0, 0, 16000, 320000, 16000
        ))
        await db.commit()
        await coordinator_for_window.shutdown()

    worker_a = JobCoordinator(lease_duration_sec=0.02, lease_heartbeat_sec=0.01)
    worker_b = JobCoordinator(lease_duration_sec=1.0, lease_heartbeat_sec=0.2)
    claim_a = await worker_a._claim_next_window(session_id, "sample-v2")
    assert claim_a is not None
    await asyncio.sleep(0.04)
    claim_b = await worker_b._claim_next_window(session_id, "sample-v2")
    assert claim_b is not None
    assert claim_b.active_attempt_id != claim_a.active_attempt_id

    engine = MagicMock()
    engine.actual_device = "cpu"
    engine.actual_compute_type = "int8"
    engine.transcribe_window.return_value = [
        HypothesisWord(start_ms=0, end_ms=500, text="stale")
    ]
    reconciler = WordContinuityReconciler(session_id)
    reconciler.default_speaker_id = "speaker"
    with pytest.raises(LostLeaseError, match="Stale completion rejected"):
        await worker_a._process_window(
            session_id,
            claim_a,
            reconciler,
            engine,
            "auto-mixed",
            "speaker",
        )

    async with AsyncSessionLocal() as db:
        window = await db.get(InferenceWindowModel, claim_b.id)
        assert window.status == "running"
        assert window.lease_owner == worker_b.coordinator_id
        assert window.active_attempt_id == claim_b.active_attempt_id
        stale_attempt = await db.get(InferenceAttemptModel, claim_a.active_attempt_id)
        assert stale_attempt.status == "superseded"
        outbox_count = (await db.execute(
            select(func.count()).select_from(OutboxEventModel)
        )).scalar()
        assert outbox_count == 0

    await worker_a.shutdown()
    await worker_b.shutdown()
