"""Hermetic pytest environment backed by temporary SQLite and storage roots."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest


_test_root_owner = tempfile.TemporaryDirectory(prefix="start-tests-")
_test_root = Path(_test_root_owner.name)
os.environ["START_DATA_DIR"] = str(_test_root / "data")
os.environ["START_SESSIONS_DIR"] = str(_test_root / "data" / "sessions")
os.environ["START_MODELS_DIR"] = str(_test_root / "data" / "models")
os.environ["START_EXPORTS_DIR"] = str(_test_root / "data" / "exports")
os.environ["START_DATABASE_URL"] = f"sqlite+aiosqlite:///{_test_root / 'test.db'}"

from app.adapters.storage.database import engine, init_db  # noqa: E402
from app.domain.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
async def isolated_database():
    for storage_root in (_test_root / "data" / "sessions", _test_root / "data" / "exports"):
        if storage_root.exists():
            shutil.rmtree(storage_root)
        storage_root.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await init_db()
    yield

    # Stop a background API-created job before the next test drops its tables.
    from app.application.job_coordinator import coordinator

    if coordinator.active_task and not coordinator.active_task.done():
        coordinator.active_task.cancel()
        await asyncio.gather(coordinator.active_task, return_exceptions=True)
    try:
        await coordinator.inference_worker.wait_idle()
    except Exception:
        pass
    coordinator.active_task = None
    coordinator.active_session_id = None
    coordinator.active_adapters.clear()
    coordinator._is_shutting_down = False
