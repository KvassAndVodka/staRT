"""Lazy, optional pyannote Community-1 diarization adapter."""
from __future__ import annotations

import asyncio
import gc
import math
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from app.config import settings
from app.ports.diarization import (
    DiarizationCapabilities,
    DiarizationEngine,
    DiarizationError,
    DiarizationSegment,
)


PipelineLoader = Callable[[Path, Path], Any]


def _default_pipeline_loader(
    model_source: Path,
    cache_dir: Path,
) -> Any:
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        raise DiarizationError(
            "Final diarization needs the optional 'diarization' dependency group"
        ) from None

    try:
        return Pipeline.from_pretrained(
            model_source,
            token=False,
            cache_dir=cache_dir,
        )
    except Exception:
        raise DiarizationError("The configured diarization model could not load") from None


class PyannoteCommunityEngine(DiarizationEngine):
    """Run Community-1 after ASR has released its model resources."""

    def __init__(
        self,
        *,
        model_source: Path,
        model_id: str = "pyannote-community-1",
        cache_dir: Path,
        device: str = "cuda",
        telemetry_enabled: bool = False,
        pipeline_loader: PipelineLoader | None = None,
    ) -> None:
        normalized_device = device.strip().lower()
        if normalized_device not in {"cpu", "cuda"}:
            raise ValueError("The diarization device must be 'cpu' or 'cuda'")
        if not model_id.strip():
            raise ValueError("The diarization model ID must not be empty")

        self.model_source = model_source
        self.model_id = model_id.strip()
        self.cache_dir = cache_dir
        self.requested_device = normalized_device
        self.actual_device = "cpu"
        self.telemetry_enabled = telemetry_enabled
        self._pipeline_loader = pipeline_loader or _default_pipeline_loader
        self._pipeline: Any | None = None
        self._lock = asyncio.Lock()

    def capabilities(self) -> DiarizationCapabilities:
        return DiarizationCapabilities(
            max_speakers=None,
            supports_overlap=True,
            device=self.actual_device,
        )

    def _create_pipeline(self) -> Any:
        try:
            pipeline = self._pipeline_loader(
                self.model_source,
                self.cache_dir,
            )
        except DiarizationError:
            raise
        except Exception:
            raise DiarizationError("The configured diarization model could not load") from None
        if pipeline is None:
            raise DiarizationError("The configured diarization model could not load")
        return pipeline

    def _load_pipeline(self, *, force_cpu: bool = False) -> Any:
        os.environ["PYANNOTE_METRICS_ENABLED"] = "1" if self.telemetry_enabled else "0"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["HF_HUB_OFFLINE"] = "1"
        if not self.model_source.exists():
            raise DiarizationError("The configured local diarization model is missing")
        if not self.model_source.is_dir() and not self.model_source.is_file():
            raise DiarizationError("The configured diarization model source is invalid")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pipeline = self._create_pipeline()
        self.actual_device = "cpu"
        if self.requested_device == "cuda" and not force_cpu:
            torch_module = None
            try:
                import torch

                torch_module = torch
                pipeline.to(torch.device("cuda"))
                self.actual_device = "cuda"
            except Exception:
                pipeline = None
                try:
                    if torch_module is not None and torch_module.cuda.is_available():
                        torch_module.cuda.empty_cache()
                except Exception:
                    pass
                pipeline = self._create_pipeline()
                self.actual_device = "cpu"
        self._pipeline = pipeline
        return pipeline

    @staticmethod
    def _is_cuda_failure(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(term in message for term in ("cuda", "cudnn", "out of memory"))

    @staticmethod
    def _segments_from_output(output: Any, duration_ms: int) -> list[DiarizationSegment]:
        annotation = getattr(output, "speaker_diarization", None)
        if annotation is None:
            raise DiarizationError("The diarization model returned no speaker timeline")
        try:
            tracks = annotation.itertracks(yield_label=True)
            raw_segments = [
                (str(label), float(turn.start), float(turn.end))
                for turn, _track, label in tracks
            ]
        except Exception as exc:
            raise DiarizationError(
                "The diarization model returned an invalid speaker timeline"
            ) from exc

        segments: list[DiarizationSegment] = []
        for label, start_seconds, end_seconds in raw_segments:
            if not label or not math.isfinite(start_seconds) or not math.isfinite(end_seconds):
                raise DiarizationError(
                    "The diarization model returned an invalid speaker interval"
                )
            start_ms = round(start_seconds * 1000)
            end_ms = round(end_seconds * 1000)
            if start_ms < -1 or end_ms > duration_ms + 1 or end_ms <= start_ms:
                raise DiarizationError(
                    "The diarization model returned an invalid speaker interval"
                )
            segments.append(DiarizationSegment(
                machine_label=label,
                start_ms=max(0, start_ms),
                end_ms=min(duration_ms, end_ms),
            ))
        return sorted(segments, key=lambda item: (item.start_ms, item.end_ms, item.machine_label))

    def _run_pipeline(self, audio_path: Path, duration_ms: int) -> Sequence[DiarizationSegment]:
        pipeline = self._pipeline or self._load_pipeline()
        try:
            output = pipeline(str(audio_path))
        except Exception as exc:
            if self.actual_device != "cuda" or not self._is_cuda_failure(exc):
                raise DiarizationError("The diarization model could not process the audio") from exc
            self._release_pipeline()
            try:
                pipeline = self._load_pipeline(force_cpu=True)
                output = pipeline(str(audio_path))
            except DiarizationError:
                raise
            except Exception as cpu_exc:
                raise DiarizationError("The diarization model could not process the audio") from cpu_exc
        return self._segments_from_output(output, duration_ms)

    async def diarize(
        self,
        audio_path: Path,
        *,
        duration_ms: int,
        model_id: str,
    ) -> Sequence[DiarizationSegment]:
        if model_id != self.model_id:
            raise DiarizationError("The session requests an unsupported diarization model")
        if duration_ms <= 0:
            raise DiarizationError("Final diarization requires a positive audio duration")
        if not audio_path.is_file():
            raise DiarizationError("The final diarization audio asset is missing")
        async with self._lock:
            return await asyncio.to_thread(self._run_pipeline, audio_path, duration_ms)

    def _release_pipeline(self) -> None:
        released_device = self.actual_device
        self._pipeline = None
        self.actual_device = "cpu"
        if released_device != "cuda":
            return
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._release_pipeline)
            await asyncio.to_thread(gc.collect)


def build_configured_diarization_engine() -> DiarizationEngine | None:
    """Build the configured adapter without importing its optional runtime."""
    if not settings.ENABLE_FINAL_DIARIZATION:
        return None
    return PyannoteCommunityEngine(
        model_source=settings.final_diarization_model_path,
        model_id=settings.DEFAULT_DIARIZATION_MODEL,
        cache_dir=settings.MODELS_DIR / "pyannote",
        device=settings.FINAL_DIARIZATION_DEVICE,
        telemetry_enabled=settings.FINAL_DIARIZATION_TELEMETRY,
    )
