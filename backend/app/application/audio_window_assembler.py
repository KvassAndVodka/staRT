"""
Verified Audio Window Assembler for staRT.
Assembles exact, source-contiguous audio slices from durable on-disk fragments
using authoritative integer sample coordinates, strict SHA-256 validation,
exact byte alignment, and exact sample-level boundary slicing.
"""
import os
import hashlib
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
import numpy as np
from sqlalchemy import select, asc
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AudioFragmentModel

class FragmentIntegrityError(Exception):
    """Raised when a fragment file is missing, size-mismatched, odd-byte misaligned, or fails SHA-256."""
    pass

class TimelineDiscontinuityError(Exception):
    """Raised when fragments have holes/gaps, sample overlaps, or non-monotonic sequence numbers."""
    pass

class VerifiedAudioWindowAssembler:
    def __init__(self, db_session: AsyncSession, sample_rate: int = 16000):
        self.db = db_session
        self.sample_rate = sample_rate

    async def assemble(
        self,
        session_id: str,
        stream_epoch: int,
        context_start_ms: int,
        context_end_ms: int
    ) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
        """
        Assembles verified contiguous float32 samples for [context_start_ms, context_end_ms].
        Uses authoritative integer sample coordinates.
        Returns: (float32_samples, ordered_manifest)
        """
        if context_start_ms >= context_end_ms:
            raise ValueError(f"Invalid context range: {context_start_ms} >= {context_end_ms}")

        target_start_sample = int((context_start_ms / 1000.0) * self.sample_rate)
        target_end_sample = int((context_end_ms / 1000.0) * self.sample_rate)
        return await self.assemble_samples(
            session_id,
            stream_epoch,
            target_start_sample,
            target_end_sample,
            self.sample_rate,
        )

    async def assemble_samples(
        self,
        session_id: str,
        stream_epoch: int,
        target_start_sample: int,
        target_end_sample: int,
        sample_rate_hz: int = 16000,
    ) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
        """Assemble the exact half-open sample range without a millisecond round-trip."""
        if target_start_sample < 0 or target_start_sample >= target_end_sample:
            raise ValueError(
                f"Invalid sample range: {target_start_sample} >= {target_end_sample}"
            )
        if sample_rate_hz <= 0:
            raise ValueError(f"Invalid sample rate: {sample_rate_hz}")

        expected_total_samples = target_end_sample - target_start_sample

        # 1. Query only durable fragments with non-null SHA-256 overlapping the requested interval
        result = await self.db.execute(
            select(AudioFragmentModel)
            .where(AudioFragmentModel.session_id == session_id)
            .where(AudioFragmentModel.stream_epoch == stream_epoch)
            .where(AudioFragmentModel.status == "durable")
            .where(AudioFragmentModel.sha256.is_not(None))
            .where(AudioFragmentModel.sample_end > target_start_sample)
            .where(AudioFragmentModel.sample_start < target_end_sample)
            .order_by(asc(AudioFragmentModel.sample_start), asc(AudioFragmentModel.sequence))
        )
        fragments = result.scalars().all()

        if not fragments:
            raise TimelineDiscontinuityError(
                f"No durable audio fragments found for sample range [{target_start_sample}, {target_end_sample}]"
            )

        # 2. Check coverage of start and end
        if fragments[0].sample_start > target_start_sample:
            raise TimelineDiscontinuityError(
                f"Gap at start: requested sample {target_start_sample}, but first fragment starts at {fragments[0].sample_start}"
            )
        if fragments[-1].sample_end < target_end_sample:
            raise TimelineDiscontinuityError(
                f"Gap at end: requested sample {target_end_sample}, but last fragment ends at {fragments[-1].sample_end}"
            )

        # 3. Validate continuity, checksums, and sample alignment
        manifest: List[Dict[str, Any]] = []
        raw_chunks: List[np.ndarray] = []

        expected_sample_start = fragments[0].sample_start
        prev_seq = None

        for frag in fragments:
            # Monotonic sequence check
            if prev_seq is not None and frag.sequence != prev_seq + 1:
                raise TimelineDiscontinuityError(
                    f"Non-monotonic sequence: expected {prev_seq + 1}, got {frag.sequence}"
                )
            prev_seq = frag.sequence

            # Contiguous sample coordinate check
            if frag.sample_start != expected_sample_start:
                raise TimelineDiscontinuityError(
                    f"Sample gap or overlap: expected start {expected_sample_start}, got {frag.sample_start}"
                )
            expected_sample_start = frag.sample_end

            # Verify sample count math
            if frag.sample_end - frag.sample_start != frag.sample_count:
                raise FragmentIntegrityError(
                    f"Fragment {frag.sequence} sample coordinate mismatch: {frag.sample_end} - {frag.sample_start} != {frag.sample_count}"
                )

            if frag.sample_rate_hz != sample_rate_hz:
                raise FragmentIntegrityError(
                    f"Fragment {frag.sequence} sample rate mismatch: "
                    f"{frag.sample_rate_hz}Hz vs expected {sample_rate_hz}Hz"
                )

            if frag.bytes_per_sample != 2:
                raise FragmentIntegrityError(
                    f"Fragment {frag.sequence} has unsupported sample width: "
                    f"{frag.bytes_per_sample} bytes (expected PCM s16le)"
                )

            frag_path = Path(frag.path)
            if not frag_path.exists():
                raise FragmentIntegrityError(f"Fragment file missing on disk: {frag.path}")

            with open(frag_path, "rb") as f:
                data = f.read()

            bytes_per_sample = frag.bytes_per_sample
            if len(data) % bytes_per_sample != 0:
                raise FragmentIntegrityError(
                    f"Fragment {frag.sequence} has odd/misaligned byte count: {len(data)} bytes (expected multiple of {bytes_per_sample})"
                )

            expected_bytes = frag.sample_count * bytes_per_sample
            if len(data) != expected_bytes:
                raise FragmentIntegrityError(
                    f"Fragment {frag.sequence} file size mismatch: {len(data)} bytes vs expected {expected_bytes}"
                )

            computed_hash = hashlib.sha256(data).hexdigest()
            if computed_hash != frag.sha256:
                raise FragmentIntegrityError(
                    f"Checksum mismatch for fragment {frag.sequence}: expected {frag.sha256}, got {computed_hash}"
                )

            frag_samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

            # Slicing using exact integer sample coordinates
            slice_start = max(0, target_start_sample - frag.sample_start)
            slice_end = min(frag.sample_count, target_end_sample - frag.sample_start)

            sliced_audio = frag_samples[slice_start:slice_end]
            raw_chunks.append(sliced_audio)

            manifest.append({
                "fragment_id": frag.id,
                "sequence": frag.sequence,
                "sample_start": frag.sample_start,
                "sample_end": frag.sample_end,
                "sample_count": frag.sample_count,
                "sliced_samples": len(sliced_audio),
                "sha256": frag.sha256
            })

        concatenated_audio = np.concatenate(raw_chunks)
        if len(concatenated_audio) != expected_total_samples:
            raise FragmentIntegrityError(
                f"Assembled audio duration mismatch: {len(concatenated_audio)} samples vs expected {expected_total_samples}"
            )

        return concatenated_audio, manifest
