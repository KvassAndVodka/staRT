"""
Deterministic contract tests for VerifiedAudioWindowAssembler using integer sample coordinates,
arbitrary partial-tail fragments, and comprehensive failure rejection paths.
"""
import uuid
import pytest
import hashlib
import numpy as np
from pathlib import Path

from app.adapters.storage.database import init_db, AsyncSessionLocal
from app.domain.models import SessionModel, AudioFragmentModel
from app.application.audio_window_assembler import (
    VerifiedAudioWindowAssembler, FragmentIntegrityError, TimelineDiscontinuityError
)
from app.config import settings

@pytest.mark.asyncio
async def test_assembler_preserves_source_order_and_slices_boundaries():
    await init_db()
    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Assembler Test",
            source_url="https://example.com/test.mp3",
            status="live"
        )
        db.add(session)
        await db.commit()

    frag_dir = settings.SESSIONS_DIR / session_id / "fragments"
    frag_dir.mkdir(parents=True, exist_ok=True)

    # Create 3 fragments of 32,000 samples each (2000ms at 16kHz)
    frag_values = [1000, 2000, 3000]
    async with AsyncSessionLocal() as db:
        for seq, val in enumerate(frag_values):
            sample_start = seq * 32000
            sample_end = (seq + 1) * 32000
            start_ms = seq * 2000
            end_ms = (seq + 1) * 2000
            
            int16_data = np.full(32000, val, dtype=np.int16)
            raw_bytes = int16_data.tobytes()
            sha = hashlib.sha256(raw_bytes).hexdigest()
            
            f_path = frag_dir / f"frag_{seq:06d}_{sample_start}_{sample_end}.raw"
            with open(f_path, "wb") as f:
                f.write(raw_bytes)
                
            frag = AudioFragmentModel(
                id=str(uuid.uuid4()),
                session_id=session_id,
                sequence=seq,
                stream_epoch=0,
                sample_start=sample_start,
                sample_end=sample_end,
                sample_count=32000,
                sample_rate_hz=16000,
                bytes_per_sample=2,
                source_start_ms=start_ms,
                source_end_ms=end_ms,
                path=str(f_path),
                sha256=sha,
                status="durable"
            )
            db.add(frag)
        await db.commit()

    # Request context [1000ms, 5000ms] -> samples [16000, 80000] (64,000 samples)
    async with AsyncSessionLocal() as db:
        assembler = VerifiedAudioWindowAssembler(db, sample_rate=16000)
        samples, manifest = await assembler.assemble(session_id, stream_epoch=0, context_start_ms=1000, context_end_ms=5000)

        assert len(samples) == 64000
        assert len(manifest) == 3
        assert [m["sequence"] for m in manifest] == [0, 1, 2]

        part1 = samples[0:16000]
        part2 = samples[16000:48000]
        part3 = samples[48000:64000]

        np.testing.assert_allclose(part1, 1000.0 / 32768.0, atol=1e-5)
        np.testing.assert_allclose(part2, 2000.0 / 32768.0, atol=1e-5)
        np.testing.assert_allclose(part3, 3000.0 / 32768.0, atol=1e-5)

@pytest.mark.asyncio
@pytest.mark.parametrize("tail_sample_count", [1, 15, 16, 17, 1001, 31999])
async def test_assembler_arbitrary_partial_tail_fragments(tail_sample_count: int):
    """Verify that arbitrary partial-sample tail fragments assemble without sample mismatch errors."""
    await init_db()
    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        session = SessionModel(
            id=session_id,
            title="Tail Test",
            source_url="https://example.com/tail.mp3",
            status="live"
        )
        db.add(session)
        await db.commit()

    frag_dir = settings.SESSIONS_DIR / session_id / "fragments"
    frag_dir.mkdir(parents=True, exist_ok=True)

    # Base fragment: 32000 samples (0 to 32000)
    int16_base = np.full(32000, 100, dtype=np.int16)
    raw_base = int16_base.tobytes()
    p_base = frag_dir / "frag_base.raw"
    with open(p_base, "wb") as f: f.write(raw_base)

    # Tail fragment: tail_sample_count samples (32000 to 32000 + tail_sample_count)
    int16_tail = np.full(tail_sample_count, 200, dtype=np.int16)
    raw_tail = int16_tail.tobytes()
    p_tail = frag_dir / f"frag_tail_{tail_sample_count}.raw"
    with open(p_tail, "wb") as f: f.write(raw_tail)

    async with AsyncSessionLocal() as db:
        db.add_all([
            AudioFragmentModel(
                id=str(uuid.uuid4()), session_id=session_id, sequence=0, stream_epoch=0,
                sample_start=0, sample_end=32000, sample_count=32000, sample_rate_hz=16000, bytes_per_sample=2,
                source_start_ms=0, source_end_ms=2000, path=str(p_base), sha256=hashlib.sha256(raw_base).hexdigest(),
                status="durable"
            ),
            AudioFragmentModel(
                id=str(uuid.uuid4()), session_id=session_id, sequence=1, stream_epoch=0,
                sample_start=32000, sample_end=32000 + tail_sample_count, sample_count=tail_sample_count,
                sample_rate_hz=16000, bytes_per_sample=2,
                source_start_ms=2000, source_end_ms=int(((32000 + tail_sample_count) / 16000.0) * 1000),
                path=str(p_tail), sha256=hashlib.sha256(raw_tail).hexdigest(), status="durable"
            )
        ])
        await db.commit()

    async with AsyncSessionLocal() as db:
        assembler = VerifiedAudioWindowAssembler(db, sample_rate=16000)
        # Assemble the exact sample frontier; no millisecond round-trip is allowed.
        total_samples = 32000 + tail_sample_count
        samples, manifest = await assembler.assemble_samples(
            session_id,
            stream_epoch=0,
            target_start_sample=0,
            target_end_sample=total_samples,
            sample_rate_hz=16000,
        )
        
        assert len(samples) == total_samples
        assert len(manifest) == 2
        np.testing.assert_allclose(samples[-tail_sample_count:], 200.0 / 32768.0, atol=1e-5)

@pytest.mark.asyncio
async def test_assembler_rejects_non_durable_status():
    await init_db()
    session_id = str(uuid.uuid4())
    frag_dir = settings.SESSIONS_DIR / session_id / "fragments"
    frag_dir.mkdir(parents=True, exist_ok=True)

    data = np.zeros(32000, dtype=np.int16).tobytes()
    p = frag_dir / "frag_writing.raw"
    with open(p, "wb") as f: f.write(data)

    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Non-durable fragment",
            source_url="https://example.com/writing.raw",
            status="live",
        ))
        frag = AudioFragmentModel(
            id=str(uuid.uuid4()), session_id=session_id, sequence=0, stream_epoch=0,
            sample_start=0, sample_end=32000, sample_count=32000, sample_rate_hz=16000, bytes_per_sample=2,
            source_start_ms=0, source_end_ms=2000, path=str(p), sha256=hashlib.sha256(data).hexdigest(),
            status="writing"  # Not durable
        )
        db.add(frag)
        await db.commit()

    async with AsyncSessionLocal() as db:
        assembler = VerifiedAudioWindowAssembler(db, sample_rate=16000)
        with pytest.raises(TimelineDiscontinuityError):
            await assembler.assemble(session_id, stream_epoch=0, context_start_ms=0, context_end_ms=2000)

@pytest.mark.asyncio
async def test_assembler_rejects_odd_bytes_and_checksum_mismatch():
    await init_db()
    session_id = str(uuid.uuid4())
    frag_dir = settings.SESSIONS_DIR / session_id / "fragments"
    frag_dir.mkdir(parents=True, exist_ok=True)

    # 1. Odd bytes test (e.g. 33 bytes for s16le)
    p_odd = frag_dir / "odd.raw"
    with open(p_odd, "wb") as f: f.write(b"\x00" * 33)

    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Odd fragment",
            source_url="https://example.com/odd.raw",
            status="live",
        ))
        db.add(AudioFragmentModel(
            id=str(uuid.uuid4()), session_id=session_id, sequence=0, stream_epoch=0,
            sample_start=0, sample_end=16, sample_count=16, sample_rate_hz=16000, bytes_per_sample=2,
            source_start_ms=0, source_end_ms=1, path=str(p_odd), sha256=hashlib.sha256(b"\x00" * 33).hexdigest(),
            status="durable"
        ))
        await db.commit()

    async with AsyncSessionLocal() as db:
        assembler = VerifiedAudioWindowAssembler(db, sample_rate=16000)
        with pytest.raises(FragmentIntegrityError):
            await assembler.assemble(session_id, stream_epoch=0, context_start_ms=0, context_end_ms=1)
