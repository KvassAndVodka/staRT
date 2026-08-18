"""
Export REST Endpoints.
Supports formats: txt, md, srt, vtt, json.
Respects user-edited turn text (turn.edited_text) and preserves word-level machine predictions.
"""
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.adapters.storage.database import get_db
from app.domain.models import SessionModel, SpeakerModel, TranscriptTurnModel, WordModel
from app.adapters.exporters.export_service import ExportService

router = APIRouter()

@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    format: str = Query("txt", pattern="^(txt|md|srt|vtt|json)$"),
    revision: str = Query("edited", pattern="^(edited|machine)$"),
    include_timestamps: bool = True,
    include_speakers: bool = True,
    db: AsyncSession = Depends(get_db)
):
    """
    Generate and download transcript in requested format.
    If revision='edited' (default), user-edited turn text is used.
    If revision='machine', raw machine predictions are used.
    """
    result = await db.execute(
        select(SessionModel)
        .options(
            selectinload(SessionModel.speakers),
            selectinload(SessionModel.turns),
            selectinload(SessionModel.words),
            selectinload(SessionModel.audio_assets),
            selectinload(SessionModel.timeline_gaps),
        )
        .where(SessionModel.id == session_id)
    )
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    spk_map = {s.id: s for s in session.speakers}
    sorted_words = sorted(session.words, key=lambda w: w.start_ms)
    sorted_turns = sorted(session.turns, key=lambda t: t.start_ms)

    turns_data: List[Dict[str, Any]] = []
    
    if sorted_turns:
        for t in sorted_turns:
            spk = spk_map.get(t.speaker_id)
            t_words = [w for w in sorted_words if w.start_ms >= t.start_ms and w.end_ms <= t.end_ms]
            w_dicts = [{
                "id": w.id,
                "start_ms": w.start_ms,
                "end_ms": w.end_ms,
                "text": w.machine_text,
                "speaker_id": w.speaker_id,
                "confidence": w.confidence,
                "language": w.language
            } for w in t_words]
            
            machine_text = " ".join(w["text"] for w in w_dicts if w["text"])
            
            if revision == "edited" and t.edited_text is not None:
                final_text = t.edited_text
            else:
                final_text = machine_text
            
            turns_data.append({
                "id": t.id,
                "speaker_id": t.speaker_id,
                "speaker_name": spk.display_name if spk else "Speaker",
                "speaker_color": spk.color if spk else "#4f46e5",
                "start_ms": t.start_ms,
                "end_ms": t.end_ms,
                "text": final_text,
                "edited_text": t.edited_text,
                "machine_text": machine_text,
                "words": w_dicts
            })
    elif sorted_words:
        spk = session.speakers[0] if session.speakers else None
        w_dicts = [{
            "id": w.id,
            "start_ms": w.start_ms,
            "end_ms": w.end_ms,
            "text": w.machine_text,
            "speaker_id": w.speaker_id,
            "confidence": w.confidence,
            "language": w.language
        } for w in sorted_words]
        machine_text = " ".join(w["text"] for w in w_dicts if w["text"])
        turns_data.append({
            "id": "t1",
            "speaker_id": spk.id if spk else None,
            "speaker_name": spk.display_name if spk else "Speaker 1",
            "speaker_color": spk.color if spk else "#4f46e5",
            "start_ms": sorted_words[0].start_ms,
            "end_ms": sorted_words[-1].end_ms,
            "text": machine_text,
            "edited_text": None,
            "machine_text": machine_text,
            "words": w_dicts
        })

    timeline_gaps_data = [{
        "id": gap.id,
        "source_start_ms": gap.source_start_ms,
        "source_end_ms": gap.source_end_ms,
        "wall_started_at": gap.wall_started_at,
        "wall_ended_at": gap.wall_ended_at,
        "reason": gap.reason,
        "recoverable": gap.recoverable,
        "recovered": gap.recovered,
        "details": gap.details,
    } for gap in sorted(session.timeline_gaps, key=lambda item: item.wall_started_at)]
    display_turns = list(turns_data)
    for gap in timeline_gaps_data:
        if gap["source_start_ms"] is None or gap["source_end_ms"] is None:
            continue
        display_turns.append({
            "id": f"gap-{gap['id']}",
            "speaker_id": None,
            "speaker_name": "System",
            "start_ms": gap["source_start_ms"],
            "end_ms": gap["source_end_ms"],
            "text": "[audio unavailable during stream interruption]",
            "words": [],
            "is_timeline_gap": True,
        })
    display_turns.sort(key=lambda item: (item["start_ms"], item["end_ms"]))

    # Generate format output
    safe_title = "".join(c for c in session.title if c.isalnum() or c in (" ", "-", "_")).strip() or "transcript"
    
    if format == "txt":
        content = ExportService.export_txt(session.title, display_turns, include_timestamps, include_speakers)
        media_type = "text/plain; charset=utf-8"
        filename = f"{safe_title}.txt"
    elif format == "md":
        content = ExportService.export_markdown(session.title, display_turns, include_timestamps, include_speakers)
        media_type = "text/markdown; charset=utf-8"
        filename = f"{safe_title}.md"
    elif format == "srt":
        content = ExportService.export_srt(display_turns, include_speakers)
        media_type = "text/plain; charset=utf-8"
        filename = f"{safe_title}.srt"
    elif format == "vtt":
        content = ExportService.export_vtt(display_turns, include_speakers)
        media_type = "text/vtt; charset=utf-8"
        filename = f"{safe_title}.vtt"
    elif format == "json":
        speakers_data = [{
            "id": s.id,
            "machine_label": s.machine_label,
            "display_name": s.display_name,
            "color": s.color
        } for s in session.speakers]
        audio_assets_data = [{
            "id": a.id,
            "kind": a.kind,
            "status": a.status,
            "container": a.container,
            "codec": a.codec,
            "duration_ms": a.duration_ms,
            "size_bytes": a.size_bytes,
            "sha256": a.sha256
        } for a in session.audio_assets]
        session_meta = {
            "id": session.id,
            "title": session.title,
            "source_url": session.source_url,
            "duration_ms": session.duration_ms,
            "created_at": session.created_at,
            "asr_model": session.asr_model,
            "actual_asr_device": session.actual_asr_device,
            "actual_compute_type": session.actual_compute_type,
            "diarization_model": session.diarization_model,
            "language_mode": session.language_mode,
            "revision": revision,
            "audio_assets": audio_assets_data,
            "timeline_gaps": timeline_gaps_data,
        }
        content = ExportService.export_json(session_meta, turns_data, speakers_data)
        media_type = "application/json; charset=utf-8"
        filename = f"{safe_title}.json"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
