"""
Audio Streaming, Range-Capable Download, and Asset Management REST Endpoints.
Conforms to RFC 7233 Range requests with strict Path.is_relative_to containment validation.
"""
import os
import stat
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.config import settings
from app.adapters.storage.database import get_db
from app.domain.models import SessionModel, AudioAssetModel, AudioAssetSchema

router = APIRouter()

def validate_path_containment(file_path: Path):
    """Ensure file path is strictly inside the allowed sessions directory using is_relative_to."""
    try:
        resolved = file_path.resolve()
        sessions_root = settings.SESSIONS_DIR.resolve()
        if not resolved.is_relative_to(sessions_root):
            raise HTTPException(status_code=403, detail="Forbidden file path access")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden file path access")

def range_requests_response(
    request: Request,
    file_path: Path,
    content_type: str = "audio/wav"
) -> StreamingResponse:
    """Yield partial content chunks for RFC 7233 HTTP Range requests."""
    validate_path_containment(file_path)
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header or file_size == 0:
        # Full content response
        def full_iter():
            with open(file_path, "rb") as f:
                while chunk := f.read(64 * 1024):
                    yield chunk

        return StreamingResponse(
            full_iter(),
            status_code=200,
            media_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Disposition": f'inline; filename="{file_path.name}"'
            }
        )

    # Parse Range: bytes=start-end, bytes=start-, bytes=-suffix
    try:
        range_str = range_header.strip().replace("bytes=", "")
        parts = range_str.split("-")
        
        if len(parts) != 2:
            raise ValueError()
            
        start_str, end_str = parts[0].strip(), parts[1].strip()
        
        if not start_str and end_str:
            # Suffix range: bytes=-500 (last 500 bytes)
            suffix_len = int(end_str)
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        elif start_str and not end_str:
            # Open-ended: bytes=500- (from 500 to end)
            start = int(start_str)
            end = file_size - 1
        elif start_str and end_str:
            # Explicit range: bytes=500-1000 (clamped to file_size - 1)
            start = int(start_str)
            end = min(int(end_str), file_size - 1)
        else:
            raise ValueError()
            
    except Exception:
        raise HTTPException(
            status_code=416,
            detail="Requested Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"}
        )

    if start >= file_size or start > end or start < 0:
        raise HTTPException(
            status_code=416,
            detail="Requested Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"}
        )

    content_length = (end - start) + 1

    def iter_range():
        with open(file_path, "rb") as f:
            f.seek(start)
            bytes_left = content_length
            while bytes_left > 0:
                chunk_size = min(64 * 1024, bytes_left)
                data = f.read(chunk_size)
                if not data:
                    break
                bytes_left -= len(data)
                yield data

    return StreamingResponse(
        iter_range(),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Disposition": f'inline; filename="{file_path.name}"'
        }
    )

@router.get("/sessions/{session_id}/audio")
async def stream_session_audio(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Stream normalized inference audio or master audio for the session.
    Supports HTTP Range requests (206 Partial Content) for instant seeking in HTML5 audio.
    """
    s_res = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = s_res.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session_dir = settings.SESSIONS_DIR / session_id / "audio"
    inference_path = session_dir / "inference.wav"
    master_path = session_dir / "master.m4a"

    if inference_path.exists() and inference_path.stat().st_size > 44:
        return range_requests_response(request, inference_path, content_type="audio/wav")
    elif master_path.exists() and master_path.stat().st_size > 0:
        return range_requests_response(request, master_path, content_type="audio/mp4")
    else:
        raise HTTPException(status_code=404, detail="Audio not available for this session yet")

@router.get("/sessions/{session_id}/audio-assets", response_model=List[AudioAssetSchema])
async def list_session_audio_assets(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """List all registered audio assets for a session."""
    s_res = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    if not s_res.scalars().first():
        raise HTTPException(status_code=404, detail="Session not found")

    result = await db.execute(
        select(AudioAssetModel).where(AudioAssetModel.session_id == session_id)
    )
    assets = result.scalars().all()
    return [
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
            sha256=a.sha256
        ) for a in assets
    ]

@router.get("/audio-assets/{asset_id}/download")
async def download_audio_asset(
    asset_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Range-capable download for a specific audio asset with path validation."""
    result = await db.execute(
        select(AudioAssetModel).where(AudioAssetModel.id == asset_id)
    )
    asset = result.scalars().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Audio asset not found")

    file_path = Path(asset.path)
    validate_path_containment(file_path)
    content_type = "audio/wav" if asset.container == "wav" else "audio/mp4"
    return range_requests_response(request, file_path, content_type=content_type)

@router.delete("/audio-assets/{asset_id}")
async def delete_audio_asset(
    asset_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Delete one finalized audio asset with state guards."""
    result = await db.execute(
        select(AudioAssetModel).where(AudioAssetModel.id == asset_id)
    )
    asset = result.scalars().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Audio asset not found")

    if asset.status in ("writing", "finalizing"):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an audio asset that is currently being written or finalized."
        )

    file_path = Path(asset.path)
    validate_path_containment(file_path)
    
    if file_path.exists():
        os.remove(file_path)

    await db.execute(delete(AudioAssetModel).where(AudioAssetModel.id == asset_id))
    await db.commit()
    return {"status": "deleted", "asset_id": asset_id}
