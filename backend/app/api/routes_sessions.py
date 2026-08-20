"""
Session Management REST Endpoints.
Guards lifecycle transitions and handles safe database deletion.
"""
import uuid
import shutil
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc
from sqlalchemy.orm import selectinload

from app.config import settings
from app.adapters.storage.database import get_db
from app.domain.models import (
    SessionModel, SpeakerModel, WordModel, TranscriptTurnModel, AudioAssetModel,
    SessionCreateRequest, SessionSummarySchema, SessionDetailSchema, SpeakerSchema, TurnSchema, WordSchema, AudioAssetSchema
)
from app.application.job_coordinator import coordinator

router = APIRouter()

@router.post("/sessions", response_model=SessionSummarySchema, status_code=status.HTTP_201_CREATED)
async def create_session(req: SessionCreateRequest, db: AsyncSession = Depends(get_db)):
    """Create a new transcription session and queue/start ingestion."""
    session_id = str(uuid.uuid4())
    new_session = SessionModel(
        id=session_id,
        title="Connecting to source...",
        source_url=req.url.strip(),
        source_type="live",
        status="queued",
        processing_mode="normal",
        language_mode=req.language_mode,
        allowed_languages=req.allowed_languages,
        asr_model=req.asr_model or settings.DEFAULT_ASR_MODEL,
        diarization_model=req.diarization_model or "pyannote-community-1",
    )
    db.add(new_session)
    await db.commit()
    await db.refresh(new_session)

    # Launch or enqueue background job
    await coordinator.start_job(session_id)

    return SessionSummarySchema(
        id=new_session.id,
        title=new_session.title,
        source_url=new_session.source_url,
        source_type=new_session.source_type,
        status=new_session.status,
        processing_mode=new_session.processing_mode,
        language_mode=new_session.language_mode,
        duration_ms=new_session.duration_ms,
        created_at=new_session.created_at,
        updated_at=new_session.updated_at,
        asr_model=new_session.asr_model,
        diarization_model=new_session.diarization_model,
        speaker_count=1
    )

@router.get("/sessions", response_model=List[SessionSummarySchema])
async def list_sessions(db: AsyncSession = Depends(get_db)):
    """List active (non-trashed) sessions ordered by creation date descending."""
    result = await db.execute(
        select(SessionModel)
        .where(SessionModel.deleted_at.is_(None))
        .order_by(desc(SessionModel.created_at))
    )
    sessions = result.scalars().all()
    
    summaries = []
    for s in sessions:
        spk_res = await db.execute(select(SpeakerModel).where(SpeakerModel.session_id == s.id))
        speakers = spk_res.scalars().all()
        summaries.append(SessionSummarySchema(
            id=s.id,
            title=s.title,
            source_url=s.source_url,
            source_type=s.source_type,
            status=s.status,
            processing_mode=s.processing_mode,
            language_mode=s.language_mode,
            duration_ms=s.duration_ms,
            created_at=s.created_at,
            updated_at=s.updated_at,
            asr_model=s.asr_model,
            actual_asr_device=s.actual_asr_device,
            actual_compute_type=s.actual_compute_type,
            diarization_model=s.diarization_model,
            speaker_count=len(speakers),
            deleted_at=s.deleted_at
        ))
    return summaries

@router.get("/sessions/trash", response_model=List[SessionSummarySchema])
async def list_trashed_sessions(db: AsyncSession = Depends(get_db)):
    """List sessions currently in Trash."""
    result = await db.execute(
        select(SessionModel)
        .where(SessionModel.deleted_at.is_not(None))
        .order_by(desc(SessionModel.deleted_at))
    )
    sessions = result.scalars().all()
    
    summaries = []
    for s in sessions:
        summaries.append(SessionSummarySchema(
            id=s.id,
            title=s.title,
            source_url=s.source_url,
            source_type=s.source_type,
            status=s.status,
            processing_mode=s.processing_mode,
            language_mode=s.language_mode,
            duration_ms=s.duration_ms,
            created_at=s.created_at,
            updated_at=s.updated_at,
            asr_model=s.asr_model,
            diarization_model=s.diarization_model,
            speaker_count=0,
            deleted_at=s.deleted_at
        ))
    return summaries

@router.get("/sessions/{session_id}", response_model=SessionDetailSchema)
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get full session transcript and audio asset details."""
    result = await db.execute(
        select(SessionModel)
        .options(
            selectinload(SessionModel.speakers),
            selectinload(SessionModel.turns),
            selectinload(SessionModel.words),
            selectinload(SessionModel.audio_assets),
            selectinload(SessionModel.inference_windows),
        )
        .where(SessionModel.id == session_id)
    )
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    spk_map = {s.id: s for s in session.speakers}
    speakers_list = [
        SpeakerSchema(
            id=s.id,
            machine_label=s.machine_label,
            display_name=s.display_name,
            color=s.color,
            sort_order=s.sort_order
        ) for s in session.speakers
    ]

    audio_assets_list = [
        AudioAssetSchema(
            id=a.id,
            session_id=a.session_id,
            kind=a.kind,
            status=a.status,
            container=a.container,
            codec=a.codec,
            sample_rate_hz=a.sample_rate_hz,
            channels=a.channels,
            duration_ms=a.duration_ms,
            size_bytes=a.size_bytes,
            sha256=a.sha256,
            derived_from_id=a.derived_from_id,
            provenance=a.provenance,
        ) for a in session.audio_assets
    ]

    sorted_turns = sorted(session.turns, key=lambda t: t.start_ms)
    sorted_words = sorted(session.words, key=lambda w: w.start_ms)
    snapshot_words = []
    if not sorted_words:
        completed_windows = sorted(
            (
                window for window in session.inference_windows
                if window.model_profile_revision == session.active_processing_revision
                and window.status == "succeeded"
                and window.reconciler_snapshot
            ),
            key=lambda window: (window.stream_epoch, window.ordinal),
        )
        if completed_windows:
            snapshot = completed_windows[-1].reconciler_snapshot
            snapshot_words = sorted(
                (snapshot.get("committed_words", []) + snapshot.get("provisional_words", [])),
                key=lambda word: word["start_ms"],
            )
    turns_list = []
    
    if sorted_turns:
        for t in sorted_turns:
            spk = spk_map.get(t.speaker_id)
            t_words = [w for w in sorted_words if w.start_ms >= t.start_ms and w.end_ms <= t.end_ms]
            w_schemas = [
                WordSchema(
                    id=w.id,
                    start_ms=w.start_ms,
                    end_ms=w.end_ms,
                    text=w.edited_text if w.edited_text is not None else w.machine_text,
                    speaker_id=w.speaker_id,
                    stability=w.stability,
                    confidence=w.confidence,
                    language=w.language
                ) for w in t_words
            ]
            displayed_text = t.edited_text if t.edited_text is not None else (" ".join(w.text for w in w_schemas) if w_schemas else "")
            turns_list.append(TurnSchema(
                id=t.id,
                speaker_id=t.speaker_id,
                speaker_name=spk.display_name if spk else "Speaker",
                speaker_color=spk.color if spk else "#4f46e5",
                start_ms=t.start_ms,
                end_ms=t.end_ms,
                text=displayed_text,
                edited_text=t.edited_text,
                words=w_schemas,
                break_reason=t.break_reason
            ))
    elif sorted_words:
        w_schemas = [
            WordSchema(
                id=w.id,
                start_ms=w.start_ms,
                end_ms=w.end_ms,
                text=w.edited_text if w.edited_text is not None else w.machine_text,
                speaker_id=w.speaker_id,
                stability=w.stability,
                confidence=w.confidence,
                language=w.language
            ) for w in sorted_words
        ]
        spk = session.speakers[0] if session.speakers else None
        turns_list.append(TurnSchema(
            id=str(uuid.uuid4()),
            speaker_id=spk.id if spk else None,
            speaker_name=spk.display_name if spk else "Speaker 1",
            speaker_color=spk.color if spk else "#4f46e5",
            start_ms=sorted_words[0].start_ms,
            end_ms=sorted_words[-1].end_ms,
            text=" ".join(w.text for w in w_schemas),
            words=w_schemas,
            break_reason="ongoing"
        ))
    elif snapshot_words:
        w_schemas = [
            WordSchema(
                id=word["id"],
                start_ms=word["start_ms"],
                end_ms=word["end_ms"],
                text=word["text"],
                speaker_id=word.get("speaker_id"),
                stability=word.get("stability", "provisional"),
                confidence=word.get("confidence"),
                language=word.get("language"),
            ) for word in snapshot_words
        ]
        speaker = spk_map.get(snapshot_words[0].get("speaker_id"))
        turns_list.append(TurnSchema(
            id=f"snapshot-{session.id}",
            speaker_id=speaker.id if speaker else snapshot_words[0].get("speaker_id"),
            speaker_name=speaker.display_name if speaker else "Speaker 1",
            speaker_color=speaker.color if speaker else "#4f46e5",
            start_ms=w_schemas[0].start_ms,
            end_ms=w_schemas[-1].end_ms,
            text=" ".join(word.text for word in w_schemas),
            words=w_schemas,
            break_reason="ongoing",
        ))

    return SessionDetailSchema(
        id=session.id,
        title=session.title,
        source_url=session.source_url,
        source_type=session.source_type,
        status=session.status,
        processing_mode=session.processing_mode,
        language_mode=session.language_mode,
        duration_ms=session.duration_ms,
        created_at=session.created_at,
        updated_at=session.updated_at,
        asr_model=session.asr_model,
        actual_asr_device=session.actual_asr_device,
        actual_compute_type=session.actual_compute_type,
        diarization_model=session.diarization_model,
        speaker_count=len(speakers_list),
        speakers=speakers_list,
        turns=turns_list,
        audio_assets=audio_assets_list,
        audio_assets_count=len(audio_assets_list),
        last_durable_audio_ms=session.last_durable_audio_ms,
        committed_frontier_ms=session.committed_frontier_ms,
        training_consent=session.training_consent,
        deleted_at=session.deleted_at
    )

@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Stop live audio ingestion and trigger finalization."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    await coordinator.stop_job(session_id)
    return {"status": "stopping", "session_id": session_id}

@router.delete("/sessions/{session_id}")
async def trash_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Move session to Trash (soft delete)."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "trashed", "session_id": session_id}

@router.post("/sessions/{session_id}/restore")
async def restore_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Restore session from Trash."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session.deleted_at = None
    await db.commit()
    return {"status": "restored", "session_id": session_id}

@router.delete("/sessions/{session_id}/purge")
async def purge_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Permanently purge session database rows and local audio/export files."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status in ("connecting", "live", "finalizing"):
        raise HTTPException(
            status_code=409,
            detail="Cannot purge a session that is actively capturing or finalizing. Stop it first."
        )

    session_dir = settings.SESSIONS_DIR / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
            
    await db.execute(delete(SessionModel).where(SessionModel.id == session_id))
    await db.commit()
    return {"status": "purged", "session_id": session_id}
