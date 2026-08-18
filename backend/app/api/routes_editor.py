"""
Transcript Editor REST Endpoints.
Handles speaker renaming, turn text editing, speaker reassignment, and consent-aware correction events
without destroying immutable machine word timestamps.
"""
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.adapters.storage.database import get_db
from app.domain.models import (
    SpeakerModel, TranscriptTurnModel, WordModel, SessionModel, CorrectionEventModel,
    SpeakerRenameRequest, TurnEditRequest
)

router = APIRouter()

@router.patch("/speakers/{speaker_id}")
async def rename_speaker(
    speaker_id: str,
    req: SpeakerRenameRequest,
    db: AsyncSession = Depends(get_db)
):
    """Rename a speaker globally across the entire session."""
    result = await db.execute(select(SpeakerModel).where(SpeakerModel.id == speaker_id))
    speaker = result.scalars().first()
    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    s_res = await db.execute(select(SessionModel).where(SessionModel.id == speaker.session_id))
    session = s_res.scalars().first()
    consent = session.training_consent if session else "excluded"

    old_name = speaker.display_name
    old_color = speaker.color
    
    speaker.display_name = req.display_name.strip()
    if req.color:
        speaker.color = req.color.strip()

    # Log immutable CorrectionEvent respecting session training consent
    event = CorrectionEventModel(
        id=str(uuid.uuid4()),
        session_id=speaker.session_id,
        target_type="speaker",
        target_id=speaker.id,
        operation="rename",
        before={"display_name": old_name, "color": old_color},
        after={"display_name": speaker.display_name, "color": speaker.color},
        training_status=consent
    )
    db.add(event)
    await db.commit()
    await db.refresh(speaker)

    return {
        "id": speaker.id,
        "display_name": speaker.display_name,
        "color": speaker.color
    }

@router.patch("/turns/{turn_id}")
async def edit_turn(
    turn_id: str,
    req: TurnEditRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Edit turn text or reassign its speaker.
    Stores versioned turn-level edited_text while preserving immutable machine word timestamps.
    """
    result = await db.execute(select(TranscriptTurnModel).where(TranscriptTurnModel.id == turn_id))
    turn = result.scalars().first()
    if not turn:
        raise HTTPException(status_code=404, detail="Turn not found")

    s_res = await db.execute(select(SessionModel).where(SessionModel.id == turn.session_id))
    session = s_res.scalars().first()
    consent = session.training_consent if session else "excluded"

    # If text is updated
    if req.text is not None:
        new_text = req.text.strip()
        w_res = await db.execute(
            select(WordModel)
            .where(WordModel.session_id == turn.session_id)
            .where(WordModel.start_ms >= turn.start_ms)
            .where(WordModel.end_ms <= turn.end_ms)
            .order_by(WordModel.start_ms)
        )
        words = w_res.scalars().all()
        old_text = turn.edited_text if turn.edited_text is not None else " ".join(w.machine_text for w in words)
        
        turn.edited_text = new_text
                
        event = CorrectionEventModel(
            id=str(uuid.uuid4()),
            session_id=turn.session_id,
            target_type="turn",
            target_id=turn.id,
            operation="text_replace",
            before={"text": old_text},
            after={"text": new_text},
            audio_start_ms=turn.start_ms,
            audio_end_ms=turn.end_ms,
            training_status=consent
        )
        db.add(event)

    # If speaker reassignment is requested
    if req.speaker_id is not None:
        old_speaker = turn.speaker_id
        turn.speaker_id = req.speaker_id
        
        event = CorrectionEventModel(
            id=str(uuid.uuid4()),
            session_id=turn.session_id,
            target_type="turn",
            target_id=turn.id,
            operation="reassign",
            before={"speaker_id": old_speaker},
            after={"speaker_id": req.speaker_id},
            audio_start_ms=turn.start_ms,
            audio_end_ms=turn.end_ms,
            training_status=consent
        )
        db.add(event)

    turn.revision += 1
    await db.commit()
    return {"status": "updated", "turn_id": turn_id}

@router.patch("/sessions/{session_id}/training-consent")
async def update_training_consent(
    session_id: str,
    consent: str,
    db: AsyncSession = Depends(get_db)
):
    """Update training consent status for session."""
    if consent not in ("excluded", "candidate", "approved", "withdrawn"):
        raise HTTPException(status_code=400, detail="Invalid training consent state")
        
    await db.execute(
        update(SessionModel)
        .where(SessionModel.id == session_id)
        .values(training_consent=consent)
    )
    await db.commit()
    return {"status": "updated", "session_id": session_id, "training_consent": consent}
