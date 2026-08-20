"""Capture-first transcription coordinator with durable, sample-exact recovery."""
from __future__ import annotations

import asyncio
import gc
import hashlib
import os
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Sequence

from sqlalchemy import select, update, delete, asc, func, and_, or_
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.adapters.storage.database import AsyncSessionLocal
from app.adapters.storage.fragment_files import (
    quarantine_fragment,
    reconcile_unreferenced_fragment_files,
    verify_fragment_file,
)
from app.domain.models import (
    SessionModel,
    SpeakerModel,
    WordModel,
    TranscriptTurnModel,
    AudioAssetModel,
    AudioFragmentModel,
    TimelineGapModel,
    InferenceWindowModel,
    InferenceAttemptModel,
    OutboxEventModel,
)
from app.adapters.ingestion.stream_capture import (
    StreamIngestionAdapter,
    CapturedFragment,
    SourceReconnecting,
    StreamDiscontinuity,
    IngestionError,
    IngestionSecurityError,
)
from app.adapters.asr.faster_whisper_engine import (
    FasterWhisperASREngine,
    ASREngineError,
    HypothesisWord,
)
from app.adapters.diarization import build_configured_diarization_engine
from app.application.inference_worker import InferenceWorker
from app.application.audio_window_assembler import (
    VerifiedAudioWindowAssembler,
    FragmentIntegrityError,
    TimelineDiscontinuityError,
)
from app.application.continuity import (
    WordContinuityReconciler,
    ReconciledWord,
)
from app.application.event_stream import allocate_event_sequences
from app.application.speaker_pipeline import FinalSpeakerPipeline, FinalSpeakerResult
from app.ports.diarization import DiarizationEngine, DiarizationError, DiarizationSegment
from app.api.websocket import ws_manager


@dataclass(frozen=True)
class LagPolicy:
    catching_up_threshold_items: int = 5
    record_only_threshold_items: int = 80


@dataclass(frozen=True)
class VerifiedEpochLedger:
    sample_rate: int
    frontiers: Dict[int, int]
    source_offsets_ms: Dict[int, int]
    total_duration_ms: int


@dataclass(frozen=True)
class DiarizationTimelineSpan:
    audio_start_ms: int
    audio_end_ms: int
    source_start_ms: int
    source_end_ms: int


class ReadinessValidationError(Exception):
    pass


class LostLeaseError(Exception):
    pass


class RecoveryError(Exception):
    pass


class JobCoordinator:
    def __init__(
        self,
        lag_policy: Optional[LagPolicy] = None,
        *,
        session_factory=None,
        event_publisher=None,
        inference_worker: Optional[InferenceWorker] = None,
        diarization_engine: Optional[DiarizationEngine] = None,
        speaker_pipeline: Optional[FinalSpeakerPipeline] = None,
        lease_duration_sec: float = 30.0,
        lease_heartbeat_sec: float = 10.0,
    ):
        self.coordinator_id = f"coord_{uuid.uuid4().hex[:12]}"
        self.active_session_id: Optional[str] = None
        self.active_task: Optional[asyncio.Task] = None
        self.current_model_name: Optional[str] = None
        self.current_engine: Optional[FasterWhisperASREngine] = None
        self.active_adapters: Dict[str, StreamIngestionAdapter] = {}
        self.inference_worker = inference_worker or InferenceWorker()
        self.diarization_engine = diarization_engine
        self.speaker_pipeline = speaker_pipeline or FinalSpeakerPipeline()
        self.lag_policy = lag_policy or LagPolicy()
        self.session_factory = session_factory or AsyncSessionLocal
        self.event_publisher = event_publisher or ws_manager
        self.lease_duration_sec = lease_duration_sec
        self.lease_heartbeat_sec = min(lease_heartbeat_sec, lease_duration_sec / 2)
        self._lock = asyncio.Lock()
        self._is_shutting_down = False

    @staticmethod
    def _sample_to_ms(sample: int, rate: int) -> int:
        return int(sample * 1000 / rate)

    async def get_asr_engine(self, model_name: Optional[str] = None) -> FasterWhisperASREngine:
        name = model_name or settings.DEFAULT_ASR_MODEL
        if self.current_engine is None or self.current_model_name != name:
            await self.inference_worker.wait_idle()
            if self.current_engine is not None:
                await asyncio.to_thread(self.current_engine.close)
            self.current_engine = None
            self.current_model_name = None
            gc.collect()
            engine = await asyncio.to_thread(FasterWhisperASREngine, name)
            self.current_engine = engine
            self.current_model_name = name
        return self.current_engine

    async def startup_recovery(self) -> None:
        """Reclaim abandoned attempts and schedule verified recovery jobs."""
        await self._reconcile_fragment_storage()
        now = datetime.now(timezone.utc)
        async with self.session_factory() as db:
            expired_result = await db.execute(
                select(InferenceWindowModel)
                .where(InferenceWindowModel.status == "running")
                .where(or_(
                    InferenceWindowModel.lease_expires_at.is_(None),
                    InferenceWindowModel.lease_expires_at < now,
                ))
            )
            expired = expired_result.scalars().all()
            for window in expired:
                if window.active_attempt_id:
                    await db.execute(
                        update(InferenceAttemptModel)
                        .where(InferenceAttemptModel.id == window.active_attempt_id)
                        .where(InferenceAttemptModel.status == "running")
                        .values(status="superseded", completed_at=now)
                    )
                window.status = "pending"
                window.lease_owner = None
                window.lease_expires_at = None
                window.active_attempt_id = None

            interrupted_result = await db.execute(
                select(SessionModel)
                .where(SessionModel.status.in_(["connecting", "live", "finalizing", "recovering_source"]))
                .where(SessionModel.deleted_at.is_(None))
            )
            for session in interrupted_result.scalars().all():
                fragment_status_result = await db.execute(
                    select(AudioFragmentModel.status)
                    .where(AudioFragmentModel.session_id == session.id)
                )
                fragment_statuses = fragment_status_result.scalars().all()
                if "durable" in fragment_statuses:
                    session.status = "recovering_source"
                    session.processing_mode = "recovering_source"
                    session.error_code = None
                else:
                    session.status = "failed"
                    session.error_code = (
                        "server_interrupted_no_verified_audio"
                        if fragment_statuses
                        else "server_interrupted_no_durable_audio"
                    )
                    await db.execute(
                        update(AudioAssetModel)
                        .where(AudioAssetModel.session_id == session.id)
                        .where(AudioAssetModel.status == "writing")
                        .values(status="failed")
                    )
            await db.commit()

        async with self.session_factory() as db:
            unpublished_result = await db.execute(
                select(OutboxEventModel.session_id)
                .where(OutboxEventModel.published_at.is_(None))
                .distinct()
            )
            unpublished_sessions = unpublished_result.scalars().all()
        for unpublished_session_id in unpublished_sessions:
            await self._publish_outbox(unpublished_session_id)

        await self._check_and_start_next_job()

    async def _reconcile_fragment_storage(self) -> None:
        """Repair crash artifacts before any session is considered recoverable."""
        sessions_root = Path(os.path.abspath(settings.SESSIONS_DIR))
        async with self.session_factory() as db:
            result = await db.execute(select(AudioFragmentModel))
            fragments = result.scalars().all()
            for fragment in fragments:
                if fragment.status != "durable":
                    continue
                path = Path(fragment.path)
                expected_size = fragment.sample_count * fragment.bytes_per_sample
                verified = await asyncio.to_thread(
                    verify_fragment_file,
                    path,
                    expected_size=expected_size,
                    expected_sha256=fragment.sha256 or "",
                )
                if verified:
                    continue

                absolute_path = Path(os.path.abspath(path))
                if (
                    absolute_path.is_relative_to(sessions_root)
                    and (path.exists() or path.is_symlink())
                ):
                    quarantined = await asyncio.to_thread(
                        quarantine_fragment,
                        path,
                        "integrity",
                    )
                    fragment.path = str(quarantined)
                fragment.status = "corrupt"
            await db.commit()
            referenced_paths = [Path(fragment.path) for fragment in fragments]

        await asyncio.to_thread(
            reconcile_unreferenced_fragment_files,
            settings.SESSIONS_DIR,
            referenced_paths,
        )

    async def shutdown(self) -> None:
        self._is_shutting_down = True
        for adapter in list(self.active_adapters.values()):
            try:
                await adapter.stop()
            except Exception:
                pass
        if self.active_task and not self.active_task.done():
            self.active_task.cancel()
            try:
                await asyncio.wait_for(self.active_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self.active_task = None
        await self.inference_worker.close()
        if self.current_engine is not None:
            await asyncio.to_thread(self.current_engine.close)
            self.current_engine = None
            self.current_model_name = None
        if self.diarization_engine is not None:
            await self.diarization_engine.close()

    async def start_job(self, session_id: str) -> None:
        async with self._lock:
            if self.active_session_id is not None:
                return
            self.active_session_id = session_id
            self.active_task = asyncio.create_task(self._run_job(session_id))

    async def stop_job(self, session_id: str) -> None:
        async with self.session_factory() as db:
            result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
            session = result.scalars().first()
            if session and session.status == "queued":
                session.status = "cancelled"
                await db.commit()
                return
        if self.active_session_id == session_id and self.active_task:
            self.active_task.cancel()
        adapter = self.active_adapters.get(session_id)
        if adapter:
            await adapter.stop()

    async def _check_and_start_next_job(self) -> None:
        if self._is_shutting_down:
            return
        async with self._lock:
            if self.active_session_id is not None:
                return
            async with self.session_factory() as db:
                recovery_result = await db.execute(
                    select(SessionModel)
                    .where(SessionModel.status == "recovering_source")
                    .where(SessionModel.deleted_at.is_(None))
                    .order_by(asc(SessionModel.created_at))
                )
                session = recovery_result.scalars().first()
                is_recovery = session is not None
                if session is None:
                    queued_result = await db.execute(
                        select(SessionModel)
                        .where(SessionModel.status == "queued")
                        .where(SessionModel.deleted_at.is_(None))
                        .order_by(asc(SessionModel.created_at))
                    )
                    session = queued_result.scalars().first()
                if session is None:
                    return
                self.active_session_id = session.id
                target = self._resume_job if is_recovery else self._run_job
                self.active_task = asyncio.create_task(target(session.id))

    # Compatibility for older callers/tests.
    async def _check_and_start_next_queued_job(self) -> None:
        await self._check_and_start_next_job()

    async def _default_speaker_id(self, db, session_id: str) -> str:
        result = await db.execute(
            select(SpeakerModel)
            .where(SpeakerModel.session_id == session_id)
            .order_by(asc(SpeakerModel.sort_order))
        )
        speaker = result.scalars().first()
        if speaker:
            return speaker.id
        speaker = SpeakerModel(
            id=str(uuid.uuid4()),
            session_id=session_id,
            machine_label="SPEAKER_00",
            display_name="Speaker 1",
            color="#4f46e5",
            sort_order=0,
        )
        db.add(speaker)
        await db.flush()
        return speaker.id

    @staticmethod
    def _word_from_dict(data: Dict[str, Any]) -> ReconciledWord:
        return ReconciledWord(
            id=data["id"],
            start_ms=data["start_ms"],
            end_ms=data["end_ms"],
            text=data["text"],
            speaker_id=data.get("speaker_id"),
            stability=data.get("stability", "committed"),
            confidence=data.get("confidence", 0.8),
            language=data.get("language"),
        )

    @staticmethod
    def _snapshot_reconciler(reconciler: WordContinuityReconciler) -> Dict[str, Any]:
        return {
            "committed_words": [word.to_dict() for word in reconciler.committed_words],
            "provisional_words": [word.to_dict() for word in reconciler.provisional_words],
            "committed_frontier_ms": reconciler.committed_frontier_ms,
            "active_stream_epoch": reconciler.active_stream_epoch,
        }

    async def _restore_reconciler(
        self,
        session_id: str,
        revision: str,
        default_speaker_id: str,
    ) -> WordContinuityReconciler:
        reconciler = WordContinuityReconciler(session_id)
        reconciler.default_speaker_id = default_speaker_id
        async with self.session_factory() as db:
            result = await db.execute(
                select(InferenceWindowModel)
                .where(InferenceWindowModel.session_id == session_id)
                .where(InferenceWindowModel.model_profile_revision == revision)
                .where(InferenceWindowModel.status == "succeeded")
                .where(InferenceWindowModel.reconciler_snapshot.is_not(None))
                .order_by(
                    InferenceWindowModel.stream_epoch.desc(),
                    InferenceWindowModel.ordinal.desc(),
                )
                .limit(1)
            )
            window = result.scalars().first()
        if window and window.reconciler_snapshot:
            snapshot = window.reconciler_snapshot
            reconciler.committed_words = [
                self._word_from_dict(item) for item in snapshot.get("committed_words", [])
            ]
            reconciler.provisional_words = [
                self._word_from_dict(item) for item in snapshot.get("provisional_words", [])
            ]
            reconciler.committed_frontier_ms = int(snapshot.get("committed_frontier_ms", 0))
            reconciler.active_stream_epoch = snapshot.get(
                "active_stream_epoch",
                window.stream_epoch,
            )
        return reconciler

    async def _add_available_windows(
        self,
        db,
        session_id: str,
        revision: str,
        stream_epoch: int,
        frontier_sample: int,
        *,
        include_tail: bool,
        sample_rate: int,
        source_offset_ms: int = 0,
    ) -> None:
        result = await db.execute(
            select(InferenceWindowModel)
            .where(InferenceWindowModel.session_id == session_id)
            .where(InferenceWindowModel.model_profile_revision == revision)
            .where(InferenceWindowModel.stream_epoch == stream_epoch)
            .order_by(asc(InferenceWindowModel.ordinal))
        )
        existing = result.scalars().all()
        existing_by_ordinal = {window.ordinal: window for window in existing}
        stride = int(sample_rate * settings.INFERENCE_INTERVAL_SEC)
        context_size = int(sample_rate * settings.WINDOW_DURATION_SEC)
        target_start = 0
        ordinal = 0
        while target_start + stride <= frontier_sample:
            target_end = target_start + stride
            if ordinal not in existing_by_ordinal:
                db.add(self._new_window(
                    session_id,
                    revision,
                    stream_epoch,
                    ordinal,
                    target_start,
                    target_end,
                    context_size,
                    sample_rate,
                    source_offset_ms,
                ))
            target_start = target_end
            ordinal += 1
        if include_tail and target_start < frontier_sample:
            if ordinal not in existing_by_ordinal:
                db.add(self._new_window(
                    session_id,
                    revision,
                    stream_epoch,
                    ordinal,
                    target_start,
                    frontier_sample,
                    context_size,
                    sample_rate,
                    source_offset_ms,
                ))

    def _new_window(
        self,
        session_id: str,
        revision: str,
        stream_epoch: int,
        ordinal: int,
        target_start: int,
        target_end: int,
        context_size: int,
        sample_rate: int,
        source_offset_ms: int = 0,
    ) -> InferenceWindowModel:
        context_start = max(0, target_end - context_size)
        return InferenceWindowModel(
            id=str(uuid.uuid4()),
            session_id=session_id,
            model_profile_revision=revision,
            stream_epoch=stream_epoch,
            ordinal=ordinal,
            target_start_sample=target_start,
            target_end_sample=target_end,
            context_start_sample=context_start,
            context_end_sample=target_end,
            sample_rate_hz=sample_rate,
            target_start_ms=source_offset_ms + self._sample_to_ms(target_start, sample_rate),
            target_end_ms=source_offset_ms + self._sample_to_ms(target_end, sample_rate),
            context_start_ms=source_offset_ms + self._sample_to_ms(context_start, sample_rate),
            context_end_ms=source_offset_ms + self._sample_to_ms(target_end, sample_rate),
            status="pending",
        )

    async def _claim_next_window(
        self,
        session_id: str,
        revision: Optional[str] = None,
    ) -> Optional[InferenceWindowModel]:
        now = datetime.now(timezone.utc)
        lease_expiry = now + timedelta(seconds=self.lease_duration_sec)
        async with self.session_factory() as db:
            if revision is None:
                session = await db.get(SessionModel, session_id)
                revision = session.active_processing_revision if session else "sample-v2"
            result = await db.execute(
                select(InferenceWindowModel)
                .where(InferenceWindowModel.session_id == session_id)
                .where(InferenceWindowModel.model_profile_revision == revision)
                .where(or_(
                    InferenceWindowModel.status == "pending",
                    and_(
                        InferenceWindowModel.status == "running",
                        InferenceWindowModel.lease_expires_at < now,
                    ),
                ))
                .order_by(
                    asc(InferenceWindowModel.stream_epoch),
                    asc(InferenceWindowModel.ordinal),
                )
                .limit(1)
            )
            candidate = result.scalars().first()
            if candidate is None:
                return None
            candidate_id = candidate.id
            superseded_attempt_id = (
                candidate.active_attempt_id if candidate.status == "running" else None
            )

            attempt_id = str(uuid.uuid4())
            updated = await db.execute(
                update(InferenceWindowModel)
                .where(InferenceWindowModel.id == candidate_id)
                .where(or_(
                    InferenceWindowModel.status == "pending",
                    and_(
                        InferenceWindowModel.status == "running",
                        InferenceWindowModel.lease_expires_at < now,
                    ),
                ))
                .values(
                    status="running",
                    lease_owner=self.coordinator_id,
                    lease_expires_at=lease_expiry,
                    attempt_count=InferenceWindowModel.attempt_count + 1,
                    active_attempt_id=attempt_id,
                    started_at=now,
                    error_code=None,
                )
                .execution_options(synchronize_session=False)
            )
            if updated.rowcount != 1:
                await db.rollback()
                return None
            if superseded_attempt_id:
                await db.execute(
                    update(InferenceAttemptModel)
                    .where(InferenceAttemptModel.id == superseded_attempt_id)
                    .where(InferenceAttemptModel.status == "running")
                    .values(status="superseded", completed_at=now)
                )
            item_result = await db.execute(
                select(InferenceWindowModel)
                .where(InferenceWindowModel.id == candidate_id)
                .execution_options(populate_existing=True)
            )
            item = item_result.scalars().one()
            db.add(InferenceAttemptModel(
                id=attempt_id,
                window_id=item.id,
                attempt_number=item.attempt_count,
                worker_id=self.coordinator_id,
                status="running",
                started_at=now,
            ))
            await db.commit()
            return item

    async def _lease_heartbeat(
        self,
        window_id: str,
        attempt_id: str,
        stop_event: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.lease_heartbeat_sec)
                return
            except asyncio.TimeoutError:
                pass
            expiry = datetime.now(timezone.utc) + timedelta(seconds=self.lease_duration_sec)
            async with self.session_factory() as db:
                result = await db.execute(
                    update(InferenceWindowModel)
                    .where(InferenceWindowModel.id == window_id)
                    .where(InferenceWindowModel.status == "running")
                    .where(InferenceWindowModel.lease_owner == self.coordinator_id)
                    .where(InferenceWindowModel.active_attempt_id == attempt_id)
                    .values(lease_expires_at=expiry)
                )
                await db.commit()
                if result.rowcount != 1:
                    lease_lost.set()
                    return

    async def _publish_outbox(self, session_id: str, window_id: Optional[str] = None) -> None:
        async with self.session_factory() as db:
            statement = (
                select(OutboxEventModel)
                .where(OutboxEventModel.session_id == session_id)
                .where(OutboxEventModel.published_at.is_(None))
                .order_by(asc(OutboxEventModel.sequence))
            )
            if window_id is not None:
                statement = statement.where(OutboxEventModel.window_id == window_id)
            result = await db.execute(statement)
            events = result.scalars().all()
            for event in events:
                payload = dict(event.payload)
                payload["event_id"] = event.id
                await self.event_publisher.broadcast_event(
                    event.session_id,
                    event.event_type,
                    payload,
                )
                event.published_at = datetime.now(timezone.utc)
                await db.commit()

    async def _process_window(
        self,
        session_id: str,
        work: InferenceWindowModel,
        reconciler: WordContinuityReconciler,
        asr: FasterWhisperASREngine,
        language_mode: str,
        default_speaker_id: str,
    ) -> None:
        attempt_id = work.active_attempt_id
        if not attempt_id:
            raise LostLeaseError(f"Window {work.id} has no active attempt")
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._lease_heartbeat(work.id, attempt_id, heartbeat_stop, lease_lost)
        )
        try:
            async with self.session_factory() as db:
                assembler = VerifiedAudioWindowAssembler(db, sample_rate=work.sample_rate_hz)
                samples, manifest = await assembler.assemble_samples(
                    session_id,
                    work.stream_epoch,
                    work.context_start_sample,
                    work.context_end_sample,
                    work.sample_rate_hz,
                )
            hypotheses = await self.inference_worker.run(
                asr,
                samples,
                work.context_start_ms,
                language_mode,
            )
        finally:
            heartbeat_stop.set()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        if lease_lost.is_set():
            raise LostLeaseError(f"Lease lost for window {work.id}")

        boundary_committed = reconciler.begin_stream_epoch(work.stream_epoch)
        newly_committed, provisionals = reconciler.reconcile_window(
            hypotheses,
            current_audio_time_ms=work.target_end_ms,
            current_speaker_id=default_speaker_id,
        )
        newly_committed = boundary_committed + newly_committed
        turns = reconciler.build_turns({
            default_speaker_id: {"display_name": "Speaker 1", "color": "#4f46e5"}
        })
        snapshot = self._snapshot_reconciler(reconciler)
        raw_hypotheses = [
            {
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "text": item.text,
                "confidence": item.confidence,
                "language": item.language,
            }
            for item in hypotheses
        ]
        now = datetime.now(timezone.utc)
        events: list[tuple[str, str, Dict[str, Any]]] = []
        if newly_committed:
            events.append((
                "commit",
                "transcript.commit",
                {
                    "committed_words": [word.to_dict() for word in newly_committed],
                    "committed_frontier_ms": reconciler.committed_frontier_ms,
                },
            ))
        events.extend([
            (
                "partial",
                "transcript.partial",
                {
                    "provisional_words": [word.to_dict() for word in provisionals],
                    "current_time_ms": work.target_end_ms,
                },
            ),
            (
                "turns",
                "turn.upsert",
                {"turns": [turn.to_dict({
                    default_speaker_id: {"display_name": "Speaker 1", "color": "#4f46e5"}
                }) for turn in turns]},
            ),
        ])

        async with self.session_factory() as db:
            updated = await db.execute(
                update(InferenceWindowModel)
                .where(InferenceWindowModel.id == work.id)
                .where(InferenceWindowModel.status == "running")
                .where(InferenceWindowModel.lease_owner == self.coordinator_id)
                .where(InferenceWindowModel.active_attempt_id == attempt_id)
                .values(
                    status="succeeded",
                    lease_owner=None,
                    lease_expires_at=None,
                    active_attempt_id=None,
                    committed_attempt_id=attempt_id,
                    input_manifest=manifest,
                    raw_hypotheses=raw_hypotheses,
                    reconciler_snapshot=snapshot,
                    actual_device=asr.actual_device,
                    actual_compute_type=asr.actual_compute_type,
                    completed_at=now,
                )
            )
            if updated.rowcount != 1:
                await db.rollback()
                raise LostLeaseError(f"Stale completion rejected for window {work.id}")
            await db.execute(
                update(InferenceAttemptModel)
                .where(InferenceAttemptModel.id == attempt_id)
                .where(InferenceAttemptModel.status == "running")
                .values(
                    status="succeeded",
                    input_manifest=manifest,
                    raw_hypotheses=raw_hypotheses,
                    actual_device=asr.actual_device,
                    actual_compute_type=asr.actual_compute_type,
                    completed_at=now,
                )
            )
            event_sequences = iter(
                await allocate_event_sequences(db, session_id, len(events))
            )
            for suffix, event_type, payload in events:
                sequence = next(event_sequences)
                db.add(OutboxEventModel(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    window_id=work.id,
                    idempotency_key=f"{work.id}:{suffix}",
                    event_type=event_type,
                    sequence=sequence,
                    payload=payload,
                ))
            await db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(
                    actual_asr_device=asr.actual_device,
                    actual_compute_type=asr.actual_compute_type,
                )
            )
            await db.commit()
        await self._publish_outbox(session_id, work.id)

    async def _fail_owned_window(self, work: InferenceWindowModel, exc: Exception) -> None:
        if not work.active_attempt_id:
            return
        now = datetime.now(timezone.utc)
        async with self.session_factory() as db:
            updated = await db.execute(
                update(InferenceWindowModel)
                .where(InferenceWindowModel.id == work.id)
                .where(InferenceWindowModel.status == "running")
                .where(InferenceWindowModel.lease_owner == self.coordinator_id)
                .where(InferenceWindowModel.active_attempt_id == work.active_attempt_id)
                .values(
                    status="failed",
                    lease_owner=None,
                    lease_expires_at=None,
                    active_attempt_id=None,
                    error_code=str(exc)[:100],
                    completed_at=now,
                )
            )
            if updated.rowcount == 1:
                await db.execute(
                    update(InferenceAttemptModel)
                    .where(InferenceAttemptModel.id == work.active_attempt_id)
                    .where(InferenceAttemptModel.status == "running")
                    .values(status="failed", error_code=str(exc)[:100], completed_at=now)
                )
            await db.commit()

    async def _drain_pending_windows(
        self,
        session_id: str,
        revision: str,
        reconciler: WordContinuityReconciler,
        language_mode: str,
        default_speaker_id: str,
        requested_model: Optional[str] = None,
    ) -> None:
        asr = await self.get_asr_engine(requested_model)
        while True:
            work = await self._claim_next_window(session_id, revision)
            if work is None:
                break
            try:
                await self._process_window(
                    session_id,
                    work,
                    reconciler,
                    asr,
                    language_mode,
                    default_speaker_id,
                )
            except LostLeaseError:
                raise
            except Exception as exc:
                await self._fail_owned_window(work, exc)
                raise

    @staticmethod
    def _validate_epoch_gap_chain(
        epoch_bounds: Dict[int, tuple[int, int]],
        gaps: List[TimelineGapModel],
        error_type,
    ) -> None:
        epochs = sorted(epoch_bounds)
        if epochs != list(range(len(epochs))):
            raise error_type(f"Stream epochs must be contiguous from zero: {epochs}")
        if len(gaps) != max(0, len(epochs) - 1):
            raise error_type(
                f"Expected {max(0, len(epochs) - 1)} timeline gaps, found {len(gaps)}"
            )

        by_transition: Dict[tuple[int, int], TimelineGapModel] = {}
        for gap in gaps:
            details = gap.details or {}
            transition = (
                details.get("previous_stream_epoch"),
                details.get("next_stream_epoch"),
            )
            if transition[0] is None or transition[1] is None:
                raise error_type(f"Timeline gap {gap.id} has no epoch transition metadata")
            if transition in by_transition:
                raise error_type(f"Duplicate timeline gap for transition {transition}")
            by_transition[transition] = gap

        for previous_epoch, next_epoch in zip(epochs, epochs[1:]):
            gap = by_transition.get((previous_epoch, next_epoch))
            if gap is None:
                raise error_type(
                    f"Missing timeline gap between epochs {previous_epoch} and {next_epoch}"
                )
            previous_end = epoch_bounds[previous_epoch][1]
            next_start = epoch_bounds[next_epoch][0]
            if gap.source_start_ms != previous_end or gap.source_end_ms != next_start:
                raise error_type(
                    f"Gap {gap.id} does not map epoch boundary "
                    f"{previous_end}..{next_start}"
                )
            if next_start < previous_end:
                raise error_type(
                    f"Epoch {next_epoch} moves backward after epoch {previous_epoch}"
                )
            if gap.wall_ended_at is None or gap.wall_ended_at < gap.wall_started_at:
                raise error_type(f"Timeline gap {gap.id} has invalid wall-clock bounds")

    async def _verify_fragments(self, session_id: str) -> VerifiedEpochLedger:
        async with self.session_factory() as db:
            result = await db.execute(
                select(AudioFragmentModel)
                .where(AudioFragmentModel.session_id == session_id)
                .where(AudioFragmentModel.status == "durable")
                .order_by(asc(AudioFragmentModel.stream_epoch), asc(AudioFragmentModel.sequence))
            )
            fragments = result.scalars().all()
            if not fragments:
                raise RecoveryError("No durable fragments available for recovery")
            gap_result = await db.execute(
                select(TimelineGapModel)
                .where(TimelineGapModel.session_id == session_id)
                .order_by(asc(TimelineGapModel.wall_started_at))
            )
            gaps = gap_result.scalars().all()
            sample_rate = fragments[0].sample_rate_hz
            assembler = VerifiedAudioWindowAssembler(db, sample_rate=sample_rate)
            grouped: Dict[int, List[AudioFragmentModel]] = {}
            for fragment in fragments:
                grouped.setdefault(fragment.stream_epoch, []).append(fragment)

            frontiers: Dict[int, int] = {}
            offsets: Dict[int, int] = {}
            bounds: Dict[int, tuple[int, int]] = {}
            total_duration_ms = 0
            for epoch in sorted(grouped):
                expected_start = 0
                expected_sequence = 0
                epoch_start_ms: Optional[int] = None
                epoch_end_ms = 0
                for fragment in grouped[epoch]:
                    if fragment.sample_rate_hz != sample_rate:
                        raise RecoveryError("Sample rate changes between epochs are unsupported")
                    if fragment.sequence != expected_sequence:
                        raise RecoveryError(
                            f"Epoch {epoch} sequence gap: expected {expected_sequence}, "
                            f"got {fragment.sequence}"
                        )
                    if fragment.sample_start != expected_start:
                        raise TimelineDiscontinuityError(
                            f"Epoch {epoch} recovery gap: expected sample {expected_start}, "
                            f"got {fragment.sample_start}"
                        )
                    inferred_offset = fragment.source_start_ms - self._sample_to_ms(
                        fragment.sample_start,
                        sample_rate,
                    )
                    if epoch_start_ms is None:
                        epoch_start_ms = inferred_offset
                        offsets[epoch] = inferred_offset
                    if inferred_offset != offsets[epoch]:
                        raise RecoveryError(f"Epoch {epoch} has inconsistent source mapping")
                    expected_source_end = offsets[epoch] + self._sample_to_ms(
                        fragment.sample_end,
                        sample_rate,
                    )
                    if fragment.source_end_ms != expected_source_end:
                        raise RecoveryError(f"Epoch {epoch} has inconsistent source end mapping")
                    await assembler.assemble_samples(
                        session_id,
                        epoch,
                        fragment.sample_start,
                        fragment.sample_end,
                        sample_rate,
                    )
                    expected_start = fragment.sample_end
                    expected_sequence += 1
                    epoch_end_ms = fragment.source_end_ms
                assert epoch_start_ms is not None
                frontiers[epoch] = expected_start
                bounds[epoch] = (epoch_start_ms, epoch_end_ms)
                total_duration_ms = max(total_duration_ms, epoch_end_ms)

            self._validate_epoch_gap_chain(bounds, gaps, RecoveryError)
            return VerifiedEpochLedger(
                sample_rate=sample_rate,
                frontiers=frontiers,
                source_offsets_ms=offsets,
                total_duration_ms=total_duration_ms,
            )

    async def _rebuild_inference_wav(self, session_id: str, path: Path, sample_rate: int) -> None:
        async with self.session_factory() as db:
            result = await db.execute(
                select(AudioFragmentModel)
                .where(AudioFragmentModel.session_id == session_id)
                .where(AudioFragmentModel.status == "durable")
                .order_by(asc(AudioFragmentModel.stream_epoch), asc(AudioFragmentModel.sequence))
            )
            fragments = result.scalars().all()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".recovering")
        try:
            with wave.open(str(temporary), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                for fragment in fragments:
                    output.writeframes(Path(fragment.path).read_bytes())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def _finalize_assets(
        self,
        session_id: str,
        adapter: StreamIngestionAdapter,
        total_duration_ms: int,
        *,
        recovery: bool,
        sample_rate: int,
    ) -> None:
        async with self.session_factory() as db:
            result = await db.execute(
                select(AudioAssetModel).where(AudioAssetModel.session_id == session_id)
            )
            assets = result.scalars().all()
        by_kind = {asset.kind: asset for asset in assets if asset.deleted_at is None}
        if "master" not in by_kind or "inference" not in by_kind:
            raise RecoveryError("Required master/inference asset rows are missing")
        if recovery:
            await self._rebuild_inference_wav(
                session_id,
                Path(by_kind["inference"].path),
                sample_rate,
            )
        async with self.session_factory() as db:
            finalized_kinds = ["master"]
            if "playback" in by_kind:
                finalized_kinds.append("playback")
            finalized_kinds.append("inference")
            for kind in finalized_kinds:
                asset = by_kind[kind]
                path = Path(asset.path)
                info = await adapter.probe_media_file(path)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                values = {
                    "status": "ready",
                    "container": info.container,
                    "codec": info.codec,
                    "channels": info.channels,
                    "sample_rate_hz": info.sample_rate_hz,
                    "duration_ms": info.duration_ms or total_duration_ms,
                    "size_bytes": info.size_bytes,
                    "sha256": digest,
                }
                if kind == "inference":
                    values.update(container="wav", codec="pcm_s16le", channels=1, sample_rate_hz=sample_rate)
                elif kind == "playback":
                    values.update(container="mov", codec="aac")
                await db.execute(
                    update(AudioAssetModel).where(AudioAssetModel.id == asset.id).values(**values)
                )
            await db.commit()

    @staticmethod
    async def validate_ready_invariants(
        db,
        session_id: str,
        total_duration_ms: Optional[int] = None,
        *,
        total_samples: Optional[int] = None,
        epoch_frontiers: Optional[Dict[int, int]] = None,
        sample_rate: int = 16000,
        revision: Optional[str] = None,
    ) -> None:
        session = await db.get(SessionModel, session_id)
        if session is None:
            raise ReadinessValidationError("Session does not exist")
        revision = revision or session.active_processing_revision
        if epoch_frontiers is None:
            if total_samples is None:
                if total_duration_ms is None:
                    raise ReadinessValidationError("No durable frontier supplied")
                total_samples = int(total_duration_ms * sample_rate / 1000)
            epoch_frontiers = {0: total_samples}
        expected_epochs = sorted(epoch_frontiers)
        if expected_epochs != list(range(len(expected_epochs))):
            raise ReadinessValidationError(
                f"Expected epoch frontiers must be contiguous from zero: {expected_epochs}"
            )

        asset_result = await db.execute(
            select(AudioAssetModel)
            .where(AudioAssetModel.session_id == session_id)
            .where(AudioAssetModel.deleted_at.is_(None))
        )
        assets = asset_result.scalars().all()
        for kind in ("master", "inference"):
            matching = [asset for asset in assets if asset.kind == kind]
            if len(matching) != 1 or matching[0].status != "ready":
                raise ReadinessValidationError(f"Exactly one ready {kind} asset is required")

        fragment_result = await db.execute(
            select(AudioFragmentModel)
            .where(AudioFragmentModel.session_id == session_id)
            .order_by(asc(AudioFragmentModel.stream_epoch), asc(AudioFragmentModel.sample_start))
        )
        fragments = fragment_result.scalars().all()
        if not fragments:
            raise ReadinessValidationError("No audio fragments exist")
        fragments_by_epoch: Dict[int, List[AudioFragmentModel]] = {}
        for fragment in fragments:
            fragments_by_epoch.setdefault(fragment.stream_epoch, []).append(fragment)
        if sorted(fragments_by_epoch) != expected_epochs:
            raise ReadinessValidationError(
                f"Fragment epochs {sorted(fragments_by_epoch)} != expected {expected_epochs}"
            )

        epoch_offsets: Dict[int, int] = {}
        epoch_bounds: Dict[int, tuple[int, int]] = {}
        for epoch in expected_epochs:
            expected_fragment_start = 0
            expected_sequence = 0
            epoch_end_ms = 0
            for fragment in fragments_by_epoch[epoch]:
                if fragment.status != "durable" or not fragment.sha256:
                    raise ReadinessValidationError(
                        f"Epoch {epoch} fragment {fragment.sequence} is not verified durable"
                    )
                if fragment.sample_rate_hz != sample_rate:
                    raise ReadinessValidationError(f"Epoch {epoch} sample rate mismatch")
                if fragment.sequence != expected_sequence:
                    raise ReadinessValidationError(
                        f"Epoch {epoch} sequence gap: expected {expected_sequence}, "
                        f"got {fragment.sequence}"
                    )
                if fragment.sample_start != expected_fragment_start:
                    raise ReadinessValidationError(
                        f"Epoch {epoch} fragment gap: expected {expected_fragment_start}, "
                        f"got {fragment.sample_start}"
                    )
                offset = fragment.source_start_ms - JobCoordinator._sample_to_ms(
                    fragment.sample_start,
                    sample_rate,
                )
                if epoch not in epoch_offsets:
                    epoch_offsets[epoch] = offset
                if offset != epoch_offsets[epoch]:
                    raise ReadinessValidationError(
                        f"Epoch {epoch} fragment source mapping is inconsistent"
                    )
                if fragment.source_end_ms != epoch_offsets[epoch] + JobCoordinator._sample_to_ms(
                    fragment.sample_end,
                    sample_rate,
                ):
                    raise ReadinessValidationError(
                        f"Epoch {epoch} fragment source end is inconsistent"
                    )
                expected_fragment_start = fragment.sample_end
                expected_sequence += 1
                epoch_end_ms = fragment.source_end_ms
            if expected_fragment_start != epoch_frontiers[epoch]:
                raise ReadinessValidationError(
                    f"Epoch {epoch} fragment frontier {expected_fragment_start} "
                    f"!= expected {epoch_frontiers[epoch]}"
                )
            epoch_bounds[epoch] = (epoch_offsets[epoch], epoch_end_ms)

        gap_result = await db.execute(
            select(TimelineGapModel)
            .where(TimelineGapModel.session_id == session_id)
            .order_by(asc(TimelineGapModel.wall_started_at))
        )
        gaps = gap_result.scalars().all()
        JobCoordinator._validate_epoch_gap_chain(
            epoch_bounds,
            gaps,
            ReadinessValidationError,
        )
        if total_duration_ms is not None:
            actual_duration_ms = max(bound[1] for bound in epoch_bounds.values())
            if actual_duration_ms != total_duration_ms:
                raise ReadinessValidationError(
                    f"Source duration {actual_duration_ms} != expected {total_duration_ms}"
                )

        window_result = await db.execute(
            select(InferenceWindowModel)
            .where(InferenceWindowModel.session_id == session_id)
            .where(InferenceWindowModel.model_profile_revision == revision)
            .order_by(asc(InferenceWindowModel.stream_epoch), asc(InferenceWindowModel.ordinal))
        )
        windows = window_result.scalars().all()
        if not windows:
            raise ReadinessValidationError("No inference windows exist for active revision")
        windows_by_epoch: Dict[int, List[InferenceWindowModel]] = {}
        for window in windows:
            windows_by_epoch.setdefault(window.stream_epoch, []).append(window)
        if sorted(windows_by_epoch) != expected_epochs:
            raise ReadinessValidationError(
                f"Window epochs {sorted(windows_by_epoch)} != expected {expected_epochs}"
            )
        for epoch in expected_epochs:
            expected_window_start = 0
            for expected_ordinal, window in enumerate(windows_by_epoch[epoch]):
                if window.ordinal != expected_ordinal:
                    raise ReadinessValidationError(
                        f"Epoch {epoch} window ordinal gap: expected {expected_ordinal}, "
                        f"got {window.ordinal}"
                    )
                if window.status != "succeeded":
                    raise ReadinessValidationError(
                        f"Epoch {epoch} window {window.ordinal} is {window.status}"
                    )
                if window.target_start_sample != expected_window_start:
                    raise ReadinessValidationError(
                        f"Epoch {epoch} window coverage gap or overlap: "
                        f"expected {expected_window_start}, got {window.target_start_sample}"
                    )
                if (
                    window.target_start_ms
                    != epoch_offsets[epoch] + JobCoordinator._sample_to_ms(
                        window.target_start_sample,
                        sample_rate,
                    )
                    or window.target_end_ms
                    != epoch_offsets[epoch] + JobCoordinator._sample_to_ms(
                        window.target_end_sample,
                        sample_rate,
                    )
                ):
                    raise ReadinessValidationError(
                        f"Epoch {epoch} window {window.ordinal} source mapping is inconsistent"
                    )
                if (
                    not window.committed_attempt_id
                    or not window.input_manifest
                    or window.raw_hypotheses is None
                    or window.reconciler_snapshot is None
                    or window.lease_owner is not None
                    or window.active_attempt_id is not None
                ):
                    raise ReadinessValidationError(
                        f"Epoch {epoch} window {window.ordinal} has incomplete durable result state"
                    )
                expected_window_start = window.target_end_sample
            if expected_window_start != epoch_frontiers[epoch]:
                raise ReadinessValidationError(
                    f"Epoch {epoch} window frontier {expected_window_start} "
                    f"!= expected {epoch_frontiers[epoch]}"
                )

        unpublished_result = await db.execute(
            select(func.count(OutboxEventModel.id))
            .where(OutboxEventModel.session_id == session_id)
            .where(OutboxEventModel.published_at.is_(None))
        )
        if (unpublished_result.scalar() or 0) != 0:
            raise ReadinessValidationError("Unpublished transcript events remain")

    async def _persist_final_transcript(
        self,
        db,
        session_id: str,
        reconciler: WordContinuityReconciler,
        default_speaker_id: str,
    ) -> None:
        reconciler.finalize_all()
        words = reconciler.get_all_words()
        turns = reconciler.build_turns({
            default_speaker_id: {"display_name": "Speaker 1", "color": "#4f46e5"}
        })
        await db.execute(delete(WordModel).where(WordModel.session_id == session_id))
        await db.execute(delete(TranscriptTurnModel).where(TranscriptTurnModel.session_id == session_id))
        for word in words:
            db.add(WordModel(
                id=word.id,
                session_id=session_id,
                start_ms=word.start_ms,
                end_ms=word.end_ms,
                machine_text=word.text,
                speaker_id=word.speaker_id or default_speaker_id,
                stability="finalized",
                confidence=word.confidence,
                language=word.language,
            ))
        for turn in turns:
            db.add(TranscriptTurnModel(
                id=turn.id,
                session_id=session_id,
                speaker_id=turn.speaker_id or default_speaker_id,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                first_word_id=turn.words[0].id if turn.words else None,
                last_word_id=turn.words[-1].id if turn.words else None,
                break_reason=turn.break_reason,
            ))
        await db.flush()

    async def _run_final_diarization(
        self,
        session_id: str,
        duration_ms: int,
    ) -> Optional[Sequence[DiarizationSegment]]:
        if self.diarization_engine is None:
            return None
        await self.inference_worker.wait_idle()
        if self.current_engine is not None:
            await asyncio.to_thread(self.current_engine.close)
        self.current_engine = None
        self.current_model_name = None
        gc.collect()

        async with self.session_factory() as db:
            session = await db.get(SessionModel, session_id)
            if session is None:
                raise DiarizationError("The diarization session does not exist")
            result = await db.execute(
                select(AudioAssetModel)
                .where(AudioAssetModel.session_id == session_id)
                .where(AudioAssetModel.kind == "inference")
                .where(AudioAssetModel.status == "ready")
                .where(AudioAssetModel.deleted_at.is_(None))
            )
            asset = result.scalars().one_or_none()
            fragments_result = await db.execute(
                select(AudioFragmentModel)
                .where(AudioFragmentModel.session_id == session_id)
                .where(AudioFragmentModel.status == "durable")
                .order_by(
                    asc(AudioFragmentModel.stream_epoch),
                    asc(AudioFragmentModel.sequence),
                )
            )
            fragments = fragments_result.scalars().all()
        if asset is None:
            raise DiarizationError("A ready inference asset is required for final diarization")
        path = Path(asset.path)
        if not path.is_file():
            raise DiarizationError("The final diarization audio asset is missing")
        timeline = self._build_diarization_timeline(fragments, duration_ms)
        audio_duration_ms = timeline[-1].audio_end_ms if timeline else duration_ms
        try:
            raw_segments = await self.diarization_engine.diarize(
                path,
                duration_ms=audio_duration_ms,
                model_id=session.diarization_model,
            )
            return self._map_diarization_segments(raw_segments, timeline)
        finally:
            await self.diarization_engine.close()

    @staticmethod
    def _build_diarization_timeline(
        fragments: Sequence[AudioFragmentModel],
        source_duration_ms: int,
    ) -> list[DiarizationTimelineSpan]:
        if not fragments:
            return [DiarizationTimelineSpan(
                audio_start_ms=0,
                audio_end_ms=source_duration_ms,
                source_start_ms=0,
                source_end_ms=source_duration_ms,
            )]
        sample_rates = {fragment.sample_rate_hz for fragment in fragments}
        if len(sample_rates) != 1:
            raise DiarizationError("Final diarization requires one inference sample rate")
        sample_rate = sample_rates.pop()
        if sample_rate <= 0:
            raise DiarizationError("Final diarization requires a positive sample rate")

        spans: list[DiarizationTimelineSpan] = []
        audio_samples = 0
        for fragment in fragments:
            if fragment.sample_count <= 0:
                continue
            audio_start_ms = JobCoordinator._sample_to_ms(audio_samples, sample_rate)
            audio_samples += fragment.sample_count
            audio_end_ms = JobCoordinator._sample_to_ms(audio_samples, sample_rate)
            if audio_end_ms <= audio_start_ms:
                raise DiarizationError("A diarization timeline span has no audio duration")
            if fragment.source_end_ms <= fragment.source_start_ms:
                raise DiarizationError("A diarization timeline span has no source duration")
            spans.append(DiarizationTimelineSpan(
                audio_start_ms=audio_start_ms,
                audio_end_ms=audio_end_ms,
                source_start_ms=fragment.source_start_ms,
                source_end_ms=fragment.source_end_ms,
            ))
        if not spans:
            raise DiarizationError("Final diarization requires durable audio samples")
        return spans

    @staticmethod
    def _map_diarization_segments(
        segments: Sequence[DiarizationSegment],
        timeline: Sequence[DiarizationTimelineSpan],
    ) -> list[DiarizationSegment]:
        mapped: list[DiarizationSegment] = []
        for segment in segments:
            matched_ms = 0
            for span in timeline:
                overlap_start = max(segment.start_ms, span.audio_start_ms)
                overlap_end = min(segment.end_ms, span.audio_end_ms)
                if overlap_end <= overlap_start:
                    continue
                audio_duration = span.audio_end_ms - span.audio_start_ms
                source_duration = span.source_end_ms - span.source_start_ms
                source_start = span.source_start_ms + round(
                    (overlap_start - span.audio_start_ms) * source_duration / audio_duration
                )
                source_end = span.source_start_ms + round(
                    (overlap_end - span.audio_start_ms) * source_duration / audio_duration
                )
                if source_end > source_start:
                    mapped.append(DiarizationSegment(
                        machine_label=segment.machine_label,
                        start_ms=source_start,
                        end_ms=source_end,
                        confidence=segment.confidence,
                    ))
                matched_ms += overlap_end - overlap_start
            if matched_ms != segment.end_ms - segment.start_ms:
                raise DiarizationError(
                    f"Diarization interval for {segment.machine_label} is outside durable audio"
                )
        return mapped

    async def _finalize_session(
        self,
        session_id: str,
        reconciler: WordContinuityReconciler,
        default_speaker_id: str,
        adapter: StreamIngestionAdapter,
        frontier_sample: int,
        sample_rate: int,
        revision: str,
        *,
        recovery: bool,
        epoch_frontiers: Optional[Dict[int, int]] = None,
        source_offsets_ms: Optional[Dict[int, int]] = None,
        source_duration_ms: Optional[int] = None,
    ) -> None:
        epoch_frontiers = epoch_frontiers or {0: frontier_sample}
        source_offsets_ms = source_offsets_ms or {0: 0}
        last_epoch = max(epoch_frontiers)
        total_duration_ms = source_duration_ms
        if total_duration_ms is None:
            total_duration_ms = source_offsets_ms[last_epoch] + self._sample_to_ms(
                epoch_frontiers[last_epoch],
                sample_rate,
            )
        async with self.session_factory() as db:
            await db.execute(
                update(SessionModel).where(SessionModel.id == session_id).values(status="finalizing")
            )
            await db.commit()
        await self._finalize_assets(
            session_id,
            adapter,
            total_duration_ms,
            recovery=recovery,
            sample_rate=sample_rate,
        )
        diarization_segments = await self._run_final_diarization(
            session_id,
            total_duration_ms,
        )
        speaker_result: Optional[FinalSpeakerResult] = None
        async with self.session_factory() as db:
            await self._persist_final_transcript(db, session_id, reconciler, default_speaker_id)
            if diarization_segments is not None:
                speaker_result = await self.speaker_pipeline.apply(
                    db,
                    session_id,
                    diarization_segments,
                    duration_ms=total_duration_ms,
                )
            await self.validate_ready_invariants(
                db,
                session_id,
                total_duration_ms=total_duration_ms,
                epoch_frontiers=epoch_frontiers,
                sample_rate=sample_rate,
                revision=revision,
            )
            await db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(
                    status="ready",
                    processing_mode="normal",
                    duration_ms=total_duration_ms,
                    committed_frontier_ms=reconciler.committed_frontier_ms,
                    last_durable_audio_ms=total_duration_ms,
                )
            )
            await db.commit()
        if speaker_result is not None:
            await self.event_publisher.broadcast_event(
                session_id,
                "speaker.upsert",
                {
                    "speaker_count": speaker_result.speaker_count,
                    "activity_count": speaker_result.activity_count,
                },
            )
            if speaker_result.overlap_count:
                await self.event_publisher.broadcast_event(
                    session_id,
                    "overlap.upsert",
                    {
                        "overlap_count": speaker_result.overlap_count,
                        "unresolved_word_count": speaker_result.unresolved_word_count,
                    },
                )
        await self.event_publisher.broadcast_event(
            session_id,
            "session.ready",
            {"status": "ready", "duration_ms": total_duration_ms},
        )

    async def _run_job(self, session_id: str) -> None:
        adapter: Optional[StreamIngestionAdapter] = None
        capture_task: Optional[asyncio.Task] = None
        consumer_task: Optional[asyncio.Task] = None
        try:
            async with self.session_factory() as db:
                session = await db.get(SessionModel, session_id)
                if session is None:
                    return
                session.status = "connecting"
                session.processing_mode = "normal"
                revision = session.active_processing_revision
                language_mode = session.language_mode
                requested_model = session.asr_model
                default_speaker_id = await self._default_speaker_id(db, session_id)
                source_url = session.source_url
                await db.commit()

            adapter = StreamIngestionAdapter(session_id, source_url)
            self.active_adapters[session_id] = adapter
            resolved = await adapter.resolve_source()
            async with self.session_factory() as db:
                await db.execute(
                    update(SessionModel)
                    .where(SessionModel.id == session_id)
                    .values(
                        title=resolved.title,
                        duration_ms=int(resolved.duration_sec * 1000) if resolved.duration_sec else None,
                        source_type="live" if resolved.is_live else "finite",
                        status="live",
                    )
                )
                master_asset_id = str(uuid.uuid4())
                playback_asset_id = str(uuid.uuid4())
                inference_asset_id = str(uuid.uuid4())
                source_description = {
                    "source_container": resolved.container,
                    "source_codec": resolved.codec,
                }
                db.add_all([
                    AudioAssetModel(
                        id=master_asset_id,
                        session_id=session_id,
                        kind="master",
                        status="writing",
                        path=str(adapter.master_path),
                        container=adapter.master_container,
                        codec=adapter.master_codec or resolved.codec,
                        provenance={
                            **source_description,
                            "operation": adapter.master_operation,
                            "audio_transcoded": adapter.master_audio_transcoded,
                        },
                    ),
                    AudioAssetModel(
                        id=playback_asset_id,
                        session_id=session_id,
                        kind="playback",
                        status="writing",
                        path=str(adapter.playback_path),
                        container="m4a",
                        codec="aac",
                        derived_from_id=master_asset_id,
                        provenance={
                            **source_description,
                            "operation": "transcode",
                            "audio_transcoded": True,
                            "target_codec": "aac",
                            "target_bitrate": "192k",
                        },
                    ),
                    AudioAssetModel(
                        id=inference_asset_id,
                        session_id=session_id,
                        kind="inference",
                        status="writing",
                        path=str(adapter.inference_path),
                        container="wav",
                        codec="pcm_s16le",
                        sample_rate_hz=settings.INFERENCE_SAMPLE_RATE,
                        channels=1,
                        derived_from_id=master_asset_id,
                        provenance={
                            **source_description,
                            "operation": "normalize_for_inference",
                            "audio_transcoded": True,
                            "target_codec": "pcm_s16le",
                            "target_sample_rate_hz": settings.INFERENCE_SAMPLE_RATE,
                            "target_channels": 1,
                        },
                    ),
                ])
                await db.commit()

            work_event = asyncio.Event()
            capture_finished = False
            fragment_count = 0
            frontier_sample = 0
            epoch_frontiers: Dict[int, int] = {}
            epoch_source_offsets: Dict[int, int] = {}
            epoch_next_sequences: Dict[int, int] = {}
            source_duration_ms = 0
            sample_rate = settings.INFERENCE_SAMPLE_RATE
            reconciler = WordContinuityReconciler(session_id)
            reconciler.default_speaker_id = default_speaker_id

            async def capture() -> None:
                nonlocal capture_finished, fragment_count, frontier_sample, source_duration_ms
                pending_gap_id: Optional[str] = None
                try:
                    async for item in adapter.stream_pcm_fragments():
                        if isinstance(item, SourceReconnecting):
                            if item.stream_epoch not in epoch_frontiers:
                                raise IngestionError(
                                    f"Reconnect started for unknown epoch {item.stream_epoch}"
                                )
                            if pending_gap_id is not None:
                                raise IngestionError("Duplicate open source reconnect interval")
                            pending_gap_id = str(uuid.uuid4())
                            source_gap_start_ms = (
                                epoch_source_offsets[item.stream_epoch]
                                + self._sample_to_ms(
                                    epoch_frontiers[item.stream_epoch],
                                    sample_rate,
                                )
                            )
                            async with self.session_factory() as db:
                                db.add(TimelineGapModel(
                                    id=pending_gap_id,
                                    session_id=session_id,
                                    source_start_ms=source_gap_start_ms,
                                    source_end_ms=None,
                                    wall_started_at=item.wall_started_at,
                                    wall_ended_at=None,
                                    reason=item.reason,
                                    recoverable=False,
                                    recovered=False,
                                    details={
                                        "previous_stream_epoch": item.stream_epoch,
                                        "next_stream_epoch": None,
                                    },
                                ))
                                await db.execute(
                                    update(SessionModel)
                                    .where(SessionModel.id == session_id)
                                    .values(processing_mode="recovering_source")
                                )
                                await db.commit()
                            await self.event_publisher.broadcast_event(
                                session_id,
                                "source.reconnecting",
                                {
                                    "stream_epoch": item.stream_epoch,
                                    "reason": item.reason,
                                    "wall_started_at": item.wall_started_at.isoformat(),
                                },
                            )
                            continue
                        if isinstance(item, StreamDiscontinuity):
                            previous_epoch = item.previous_stream_epoch
                            if previous_epoch not in epoch_frontiers:
                                raise IngestionError(
                                    f"Discontinuity closes unknown epoch {previous_epoch}"
                                )
                            if item.next_stream_epoch != previous_epoch + 1:
                                raise IngestionError("Stream epochs must increment by exactly one")
                            expected_gap_start = epoch_source_offsets[previous_epoch] + self._sample_to_ms(
                                epoch_frontiers[previous_epoch],
                                sample_rate,
                            )
                            if (
                                item.source_start_ms != expected_gap_start
                                or item.source_end_ms < item.source_start_ms
                            ):
                                raise IngestionError("Discontinuity has invalid source bounds")
                            async with self.session_factory() as db:
                                await self._add_available_windows(
                                    db,
                                    session_id,
                                    revision,
                                    previous_epoch,
                                    epoch_frontiers[previous_epoch],
                                    include_tail=True,
                                    sample_rate=sample_rate,
                                    source_offset_ms=epoch_source_offsets[previous_epoch],
                                )
                                if pending_gap_id is not None:
                                    await db.execute(
                                        update(TimelineGapModel)
                                        .where(TimelineGapModel.id == pending_gap_id)
                                        .values(
                                            source_start_ms=item.source_start_ms,
                                            source_end_ms=item.source_end_ms,
                                            wall_ended_at=item.wall_ended_at,
                                            reason=item.reason,
                                            recoverable=item.recoverable,
                                            recovered=False,
                                            details={
                                                "previous_stream_epoch": previous_epoch,
                                                "next_stream_epoch": item.next_stream_epoch,
                                            },
                                        )
                                    )
                                else:
                                    db.add(TimelineGapModel(
                                        id=str(uuid.uuid4()),
                                        session_id=session_id,
                                        source_start_ms=item.source_start_ms,
                                        source_end_ms=item.source_end_ms,
                                        wall_started_at=item.wall_started_at,
                                        wall_ended_at=item.wall_ended_at,
                                        reason=item.reason,
                                        recoverable=item.recoverable,
                                        recovered=False,
                                        details={
                                            "previous_stream_epoch": previous_epoch,
                                            "next_stream_epoch": item.next_stream_epoch,
                                        },
                                    ))
                                await db.commit()
                            pending_gap_id = None
                            epoch_source_offsets[item.next_stream_epoch] = item.source_end_ms
                            source_duration_ms = max(source_duration_ms, item.source_end_ms)
                            async with self.session_factory() as db:
                                await db.execute(
                                    update(SessionModel)
                                    .where(SessionModel.id == session_id)
                                    .values(processing_mode="normal")
                                )
                                await db.commit()
                            await self.event_publisher.broadcast_event(
                                session_id,
                                "source.reconnected",
                                {
                                    "previous_stream_epoch": previous_epoch,
                                    "stream_epoch": item.next_stream_epoch,
                                    "gap_start_ms": item.source_start_ms,
                                    "gap_end_ms": item.source_end_ms,
                                    "reason": item.reason,
                                },
                            )
                            work_event.set()
                            continue

                        if isinstance(item, CapturedFragment):
                            seq = item.sequence
                            epoch = item.stream_epoch
                            start = item.sample_start
                            end = item.sample_end
                            count = item.sample_count
                            start_ms = item.source_start_ms
                            end_ms = item.source_end_ms
                            wall_started_at = item.wall_started_at
                            wall_ended_at = item.wall_ended_at
                            source_pts_start = item.source_pts_start
                            source_pts_end = item.source_pts_end
                            path = item.path
                            checksum = item.sha256
                        else:
                            # Compatibility for injected/legacy adapters that use the
                            # pre-epoch tuple contract.
                            seq, start, end, count, start_ms, end_ms, _, path, checksum = item
                            epoch = 0
                            wall_started_at = datetime.now(timezone.utc)
                            wall_ended_at = None
                            source_pts_start = None
                            source_pts_end = None

                        expected_start = epoch_frontiers.get(epoch, 0)
                        expected_sequence = epoch_next_sequences.get(epoch, 0)
                        if start != expected_start or seq != expected_sequence:
                            raise IngestionError(
                                f"Epoch {epoch} fragment discontinuity: expected "
                                f"sequence/sample {expected_sequence}/{expected_start}, "
                                f"got {seq}/{start}"
                            )
                        inferred_offset = start_ms - self._sample_to_ms(start, sample_rate)
                        configured_offset = epoch_source_offsets.setdefault(epoch, inferred_offset)
                        if configured_offset != inferred_offset:
                            raise IngestionError(f"Epoch {epoch} source mapping changed")
                        if end_ms != configured_offset + self._sample_to_ms(end, sample_rate):
                            raise IngestionError(f"Epoch {epoch} source end mapping changed")

                        async with self.session_factory() as db:
                            db.add(AudioFragmentModel(
                                id=str(uuid.uuid4()),
                                session_id=session_id,
                                sequence=seq,
                                stream_epoch=epoch,
                                sample_start=start,
                                sample_end=end,
                                sample_count=count,
                                sample_rate_hz=sample_rate,
                                bytes_per_sample=2,
                                source_start_ms=start_ms,
                                source_end_ms=end_ms,
                                wall_started_at=wall_started_at,
                                wall_ended_at=wall_ended_at,
                                source_pts_start=source_pts_start,
                                source_pts_end=source_pts_end,
                                path=str(path),
                                sha256=checksum,
                                status="durable",
                            ))
                            await self._add_available_windows(
                                db,
                                session_id,
                                revision,
                                epoch,
                                end,
                                include_tail=False,
                                sample_rate=sample_rate,
                                source_offset_ms=configured_offset,
                            )
                            await db.execute(
                                update(SessionModel)
                                .where(SessionModel.id == session_id)
                                .values(last_durable_audio_ms=end_ms)
                            )
                            await db.commit()
                        fragment_count += 1
                        epoch_frontiers[epoch] = end
                        epoch_next_sequences[epoch] = seq + 1
                        frontier_sample = end
                        source_duration_ms = max(source_duration_ms, end_ms)
                        work_event.set()
                    if pending_gap_id is not None:
                        raise IngestionError("Source ended before a reconnect completed")
                finally:
                    if epoch_frontiers:
                        async with self.session_factory() as db:
                            for epoch, frontier in sorted(epoch_frontiers.items()):
                                await self._add_available_windows(
                                    db,
                                    session_id,
                                    revision,
                                    epoch,
                                    frontier,
                                    include_tail=True,
                                    sample_rate=sample_rate,
                                    source_offset_ms=epoch_source_offsets[epoch],
                                )
                            await db.commit()
                    capture_finished = True
                    work_event.set()

            async def consume() -> None:
                current_mode = "normal"
                asr = await self.get_asr_engine(requested_model)
                while True:
                    async with self.session_factory() as db:
                        count_result = await db.execute(
                            select(func.count(InferenceWindowModel.id))
                            .where(InferenceWindowModel.session_id == session_id)
                            .where(InferenceWindowModel.model_profile_revision == revision)
                            .where(InferenceWindowModel.status == "pending")
                        )
                        pending = count_result.scalar() or 0
                    if pending == 0 and capture_finished:
                        return
                    next_mode = (
                        "record_only" if pending > self.lag_policy.record_only_threshold_items
                        else "catching_up" if pending > self.lag_policy.catching_up_threshold_items
                        else "normal"
                    )
                    if next_mode != current_mode:
                        current_mode = next_mode
                        async with self.session_factory() as db:
                            await db.execute(
                                update(SessionModel)
                                .where(SessionModel.id == session_id)
                                .values(processing_mode=current_mode)
                            )
                            await db.commit()
                        await self.event_publisher.broadcast_event(
                            session_id,
                            "session.status",
                            {"status": "live", "processing_mode": current_mode},
                        )
                    if current_mode == "record_only" and not capture_finished:
                        work_event.clear()
                        try:
                            await asyncio.wait_for(work_event.wait(), timeout=0.2)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    work = await self._claim_next_window(session_id, revision)
                    if work is None:
                        work_event.clear()
                        try:
                            await asyncio.wait_for(work_event.wait(), timeout=0.2)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    try:
                        await self._process_window(
                            session_id,
                            work,
                            reconciler,
                            asr,
                            language_mode,
                            default_speaker_id,
                        )
                    except LostLeaseError:
                        return
                    except Exception as exc:
                        await self._fail_owned_window(work, exc)
                        raise

            capture_task = asyncio.create_task(capture())
            consumer_task = asyncio.create_task(consume())
            done, pending_tasks = await asyncio.wait(
                [capture_task, consumer_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            error = next((task.exception() for task in done if task.exception() is not None), None)
            if error:
                await adapter.stop()
                for task in pending_tasks:
                    task.cancel()
                await asyncio.gather(capture_task, consumer_task, return_exceptions=True)
                raise error
            await asyncio.gather(*pending_tasks)
            if fragment_count == 0:
                raise IngestionError("Zero audio fragments were captured from the source")
            await self._finalize_session(
                session_id,
                reconciler,
                default_speaker_id,
                adapter,
                frontier_sample,
                sample_rate,
                revision,
                recovery=False,
                epoch_frontiers=epoch_frontiers,
                source_offsets_ms=epoch_source_offsets,
                source_duration_ms=source_duration_ms,
            )
        except LostLeaseError:
            # Another coordinator owns this work. Do not overwrite its session state.
            pass
        except asyncio.CancelledError as exc:
            await self._mark_terminal_failure(session_id, exc, adapter, cancelled=True)
        except Exception as exc:
            await self._mark_terminal_failure(session_id, exc, adapter, cancelled=False)
        finally:
            await self._finish_job(session_id)

    async def _resume_job(self, session_id: str) -> None:
        adapter: Optional[StreamIngestionAdapter] = None
        try:
            async with self.session_factory() as db:
                session = await db.get(SessionModel, session_id)
                if session is None:
                    return
                revision = session.active_processing_revision
                language_mode = session.language_mode
                requested_model = session.asr_model
                source_url = session.source_url
                default_speaker_id = await self._default_speaker_id(db, session_id)
                session.status = "recovering_source"
                session.processing_mode = "recovering_source"
                await db.commit()

            ledger = await self._verify_fragments(session_id)
            async with self.session_factory() as db:
                for epoch, frontier in sorted(ledger.frontiers.items()):
                    await self._add_available_windows(
                        db,
                        session_id,
                        revision,
                        epoch,
                        frontier,
                        include_tail=True,
                        sample_rate=ledger.sample_rate,
                        source_offset_ms=ledger.source_offsets_ms[epoch],
                    )
                await db.commit()
            reconciler = await self._restore_reconciler(
                session_id,
                revision,
                default_speaker_id,
            )
            await self._drain_pending_windows(
                session_id,
                revision,
                reconciler,
                language_mode,
                default_speaker_id,
                requested_model,
            )
            adapter = StreamIngestionAdapter(session_id, source_url)
            await self._finalize_session(
                session_id,
                reconciler,
                default_speaker_id,
                adapter,
                ledger.frontiers[max(ledger.frontiers)],
                ledger.sample_rate,
                revision,
                recovery=True,
                epoch_frontiers=ledger.frontiers,
                source_offsets_ms=ledger.source_offsets_ms,
                source_duration_ms=ledger.total_duration_ms,
            )
        except LostLeaseError:
            pass
        except asyncio.CancelledError as exc:
            await self._mark_terminal_failure(session_id, exc, adapter, cancelled=True)
        except Exception as exc:
            await self._mark_terminal_failure(session_id, exc, adapter, cancelled=False)
        finally:
            await self._finish_job(session_id)

    async def _mark_terminal_failure(
        self,
        session_id: str,
        exc: BaseException,
        adapter: Optional[StreamIngestionAdapter],
        *,
        cancelled: bool,
    ) -> None:
        if adapter:
            try:
                await adapter.stop()
            except Exception:
                pass
        try:
            await self.inference_worker.wait_idle()
        except Exception:
            pass
        if cancelled:
            code = "cancelled"
        elif isinstance(exc, IngestionSecurityError):
            code = "security_error"
        elif isinstance(exc, ASREngineError):
            code = "asr_error"
        elif isinstance(exc, (FragmentIntegrityError, TimelineDiscontinuityError, RecoveryError)):
            code = "recovery_error"
        else:
            code = "source_error"
        async with self.session_factory() as db:
            await db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(
                    status="cancelled" if cancelled else "failed",
                    error_code=f"{code}: {str(exc)[:80]}",
                )
            )
            await db.execute(
                update(AudioAssetModel)
                .where(AudioAssetModel.session_id == session_id)
                .where(AudioAssetModel.status == "writing")
                .values(status="failed")
            )
            await db.commit()
        if not cancelled:
            await self.event_publisher.broadcast_event(
                session_id,
                "session.error",
                {"status": "failed", "error": str(exc)},
            )

    async def _finish_job(self, session_id: str) -> None:
        self.active_adapters.pop(session_id, None)
        async with self._lock:
            if self.active_session_id == session_id:
                self.active_session_id = None
                self.active_task = None
        await self._check_and_start_next_job()


coordinator = JobCoordinator(diarization_engine=build_configured_diarization_engine())
