"""
Storage Summary & Health Check REST Endpoints.
"""
import os
import shutil
import ctranslate2
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.config import settings
from app.adapters.storage.database import get_db
from app.domain.models import SessionModel, StorageSummarySchema

router = APIRouter()

def get_dir_size(path) -> int:
    total = 0
    if os.path.exists(path):
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
    return total

@router.get("/storage", response_model=StorageSummarySchema)
async def get_storage_summary(db: AsyncSession = Depends(get_db)):
    """Return summary of session counts, storage bytes, and trash usage."""
    # Active sessions
    active_res = await db.execute(
        select(func.count(SessionModel.id)).where(SessionModel.deleted_at.is_(None))
    )
    active_count = active_res.scalar() or 0

    # Trashed sessions
    trashed_res = await db.execute(
        select(func.count(SessionModel.id)).where(SessionModel.deleted_at.is_not(None))
    )
    trashed_count = trashed_res.scalar() or 0

    audio_bytes = get_dir_size(settings.SESSIONS_DIR)
    export_bytes = get_dir_size(settings.EXPORTS_DIR)

    return StorageSummarySchema(
        total_sessions=active_count + trashed_count,
        active_sessions=active_count,
        trashed_sessions=trashed_count,
        total_audio_bytes=audio_bytes,
        total_export_bytes=export_bytes
    )

@router.get("/health")
async def health_check():
    """Health check returning compute device capabilities."""
    cuda_devices = ctranslate2.get_cuda_device_count()
    diarization_model_path = settings.final_diarization_model_path
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "cuda_devices": cuda_devices,
        "default_device": "cuda" if cuda_devices > 0 else "cpu",
        "default_model": settings.DEFAULT_ASR_MODEL,
        "default_compute_type": settings.DEFAULT_COMPUTE_TYPE,
        "final_diarization": {
            "enabled": settings.ENABLE_FINAL_DIARIZATION,
            "model_id": settings.DEFAULT_DIARIZATION_MODEL,
            "local_model_available": (
                diarization_model_path.is_dir() or diarization_model_path.is_file()
            ),
            "requested_device": settings.FINAL_DIARIZATION_DEVICE,
        },
    }
