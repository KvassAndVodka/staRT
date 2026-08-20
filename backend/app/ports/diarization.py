"""Typed boundary for replaceable final-session diarization engines."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


class DiarizationError(RuntimeError):
    """The diarization adapter could not produce a valid final timeline."""


@dataclass(frozen=True)
class DiarizationSegment:
    """One model speaker interval on the adapter input audio timeline."""

    machine_label: str
    start_ms: int
    end_ms: int
    confidence: float | None = None


@dataclass(frozen=True)
class DiarizationCapabilities:
    """Resource and output limits reported by an adapter."""

    max_speakers: int | None
    supports_overlap: bool
    device: str


class DiarizationEngine(Protocol):
    """Model-independent full-session diarization contract."""

    async def diarize(
        self,
        audio_path: Path,
        *,
        duration_ms: int,
    ) -> Sequence[DiarizationSegment]: ...

    def capabilities(self) -> DiarizationCapabilities: ...

    async def close(self) -> None: ...
