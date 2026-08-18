"""Crash-boundary contracts for durable fragment publication and startup repair."""
from __future__ import annotations

import hashlib
import os
import uuid
from unittest.mock import patch

import pytest

from app.adapters.storage.database import AsyncSessionLocal
from app.adapters.storage.fragment_files import (
    FragmentPublicationError,
    publish_pcm_fragment,
)
from app.application.job_coordinator import JobCoordinator
from app.config import settings
from app.domain.models import AudioFragmentModel, SessionModel


def test_atomic_fragment_publication_syncs_and_never_replaces(tmp_path):
    final_path = tmp_path / "frag_000000_0_16.raw"
    payload = b"\x01\x00" * 16
    real_fsync = os.fsync

    with patch(
        "app.adapters.storage.fragment_files.os.fsync",
        wraps=real_fsync,
    ) as fsync:
        digest = publish_pcm_fragment(final_path, payload)

    assert final_path.read_bytes() == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert fsync.call_count >= 2  # staged file and containing directory
    assert not list(tmp_path.glob("*.tmp"))

    with pytest.raises(FragmentPublicationError, match="already exists"):
        publish_pcm_fragment(final_path, b"\x02\x00" * 16)
    assert final_path.read_bytes() == payload


def test_failed_atomic_rename_removes_staging_file(tmp_path):
    final_path = tmp_path / "frag_000000_0_16.raw"
    with patch(
        "app.adapters.storage.fragment_files.os.replace",
        side_effect=OSError("injected rename failure"),
    ):
        with pytest.raises(OSError, match="injected rename failure"):
            publish_pcm_fragment(final_path, b"\x01\x00" * 16)

    assert not final_path.exists()
    assert not list(tmp_path.iterdir())


def test_directory_sync_failure_leaves_only_reconcilable_final_file(tmp_path):
    final_path = tmp_path / "frag_000000_0_16.raw"
    payload = b"\x01\x00" * 16
    real_fsync = os.fsync
    call_count = 0

    def fail_directory_sync(descriptor):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("injected directory sync failure")
        return real_fsync(descriptor)

    with patch(
        "app.adapters.storage.fragment_files.os.fsync",
        side_effect=fail_directory_sync,
    ):
        with pytest.raises(OSError, match="injected directory sync failure"):
            publish_pcm_fragment(final_path, payload)

    assert final_path.read_bytes() == payload
    assert not list(tmp_path.glob("*.tmp"))


def test_misaligned_fragment_is_never_staged(tmp_path):
    final_path = tmp_path / "odd.raw"
    with pytest.raises(FragmentPublicationError, match="not aligned"):
        publish_pcm_fragment(final_path, b"\x00" * 33)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_startup_repair_removes_staging_and_quarantines_conflicts():
    session_id = str(uuid.uuid4())
    fragment_dir = settings.SESSIONS_DIR / session_id / "fragments"
    fragment_dir.mkdir(parents=True, exist_ok=True)
    corrupt_path = fragment_dir / "frag_000000_0_16.raw"
    corrupt_path.write_bytes(b"\x02\x00" * 16)
    orphan_path = fragment_dir / "frag_000001_16_32.raw"
    orphan_path.write_bytes(b"\x03\x00" * 16)
    staging_path = fragment_dir / ".frag_000002_32_48.raw.crash.tmp"
    staging_path.write_bytes(b"\x04\x00" * 16)

    expected = b"\x01\x00" * 16
    fragment_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Crash repair",
            source_url="https://example.com/crash.raw",
            status="live",
        ))
        db.add(AudioFragmentModel(
            id=fragment_id,
            session_id=session_id,
            sequence=0,
            stream_epoch=0,
            sample_start=0,
            sample_end=16,
            sample_count=16,
            sample_rate_hz=16000,
            bytes_per_sample=2,
            source_start_ms=0,
            source_end_ms=1,
            path=str(corrupt_path),
            sha256=hashlib.sha256(expected).hexdigest(),
            status="durable",
        ))
        await db.commit()

    test_coordinator = JobCoordinator()
    await test_coordinator._reconcile_fragment_storage()

    async with AsyncSessionLocal() as db:
        fragment = await db.get(AudioFragmentModel, fragment_id)
        assert fragment.status == "corrupt"
        assert ".integrity." in fragment.path
        assert os.path.exists(fragment.path)

    assert not corrupt_path.exists()
    assert not orphan_path.exists()
    assert not staging_path.exists()
    quarantined_names = {path.name for path in (fragment_dir / "quarantine").iterdir()}
    assert any("integrity" in name for name in quarantined_names)
    assert any("orphan" in name for name in quarantined_names)
    await test_coordinator.shutdown()
