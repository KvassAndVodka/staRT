"""Boundary tests for the optional pyannote Community-1 adapter."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.adapters.diarization.pyannote_community import (
    PyannoteCommunityEngine,
    _default_pipeline_loader,
    build_configured_diarization_engine,
)
from app.config import settings
from app.ports.diarization import DiarizationError, DiarizationSegment


class FakeAnnotation:
    def __init__(self, intervals: list[tuple[float, float, str]]) -> None:
        self.intervals = intervals

    def itertracks(self, *, yield_label: bool):
        assert yield_label is True
        for start, end, label in self.intervals:
            yield SimpleNamespace(start=start, end=end), "track", label


class FakePipeline:
    def __init__(
        self,
        intervals: list[tuple[float, float, str]],
        *,
        run_error: Exception | None = None,
        move_error: Exception | None = None,
    ) -> None:
        self.intervals = intervals
        self.run_error = run_error
        self.move_error = move_error
        self.paths: list[str] = []
        self.devices: list[str] = []

    def to(self, device) -> None:
        self.devices.append(str(device))
        if self.move_error is not None:
            raise self.move_error

    def __call__(self, audio_path: str):
        self.paths.append(audio_path)
        if self.run_error is not None:
            raise self.run_error
        return SimpleNamespace(speaker_diarization=FakeAnnotation(self.intervals))


def test_default_loader_disables_hub_credentials(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, object, Path]] = []
    expected = object()

    class FakePyannotePipeline:
        @classmethod
        def from_pretrained(cls, source: Path, *, token, cache_dir: Path):
            calls.append((source, token, cache_dir))
            return expected

    pyannote_module = ModuleType("pyannote")
    pyannote_module.__path__ = []
    audio_module = ModuleType("pyannote.audio")
    audio_module.Pipeline = FakePyannotePipeline
    monkeypatch.setitem(sys.modules, "pyannote", pyannote_module)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio_module)
    source = tmp_path / "community-1"
    cache_dir = tmp_path / "cache"

    actual = _default_pipeline_loader(source, cache_dir)

    assert actual is expected
    assert calls == [(source, False, cache_dir)]


@pytest.mark.asyncio
async def test_adapter_loads_lazily_and_preserves_overlap(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PYANNOTE_METRICS_ENABLED", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    model_source = tmp_path / "community-1"
    model_source.mkdir()
    pipeline = FakePipeline([
        (0.0, 1.5, "SPEAKER_00"),
        (1.0, 2.0, "SPEAKER_01"),
    ])
    loader_calls: list[tuple[Path, Path]] = []

    def load(source: Path, cache_dir: Path):
        loader_calls.append((source, cache_dir))
        return pipeline

    engine = PyannoteCommunityEngine(
        model_source=model_source,
        cache_dir=tmp_path / "models",
        device="cpu",
        pipeline_loader=load,
    )

    assert loader_calls == []
    assert engine.capabilities().device == "cpu"
    segments = await engine.diarize(
        audio_path,
        duration_ms=2000,
        model_id="pyannote-community-1",
    )

    assert segments == [
        DiarizationSegment("SPEAKER_00", 0, 1500),
        DiarizationSegment("SPEAKER_01", 1000, 2000),
    ]
    assert loader_calls == [(
        model_source,
        tmp_path / "models",
    )]
    assert (tmp_path / "models").is_dir()
    assert os.environ["PYANNOTE_METRICS_ENABLED"] == "0"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert pipeline.paths == [str(audio_path)]
    await engine.close()
    assert engine._pipeline is None


@pytest.mark.asyncio
async def test_adapter_rejects_unconfigured_model_before_load(tmp_path: Path):
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    load_count = 0

    def load(_source: Path, _cache: Path):
        nonlocal load_count
        load_count += 1
        return FakePipeline([])

    engine = PyannoteCommunityEngine(
        model_source=tmp_path / "missing-model",
        cache_dir=tmp_path / "models",
        device="cpu",
        pipeline_loader=load,
    )

    with pytest.raises(DiarizationError, match="unsupported diarization model"):
        await engine.diarize(audio_path, duration_ms=1000, model_id="untrusted/model")
    assert load_count == 0


@pytest.mark.asyncio
async def test_adapter_rejects_missing_local_model_before_load(tmp_path: Path):
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    load_count = 0

    def load(_source: Path, _cache: Path):
        nonlocal load_count
        load_count += 1
        return FakePipeline([])

    engine = PyannoteCommunityEngine(
        model_source=tmp_path / "missing-model",
        cache_dir=tmp_path / "models",
        device="cpu",
        pipeline_loader=load,
    )

    with pytest.raises(DiarizationError, match="local diarization model is missing"):
        await engine.diarize(
            audio_path,
            duration_ms=1000,
            model_id="pyannote-community-1",
        )
    assert load_count == 0


@pytest.mark.asyncio
async def test_adapter_hides_loader_error_details(tmp_path: Path):
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    model_source = tmp_path / "community-1"
    model_source.mkdir()

    def fail_load(
        _source: Path,
        _cache: Path,
    ):
        raise RuntimeError("local loader exposed private diagnostics")

    engine = PyannoteCommunityEngine(
        model_source=model_source,
        cache_dir=tmp_path / "models",
        device="cpu",
        pipeline_loader=fail_load,
    )

    with pytest.raises(DiarizationError) as caught:
        await engine.diarize(
            audio_path,
            duration_ms=1000,
            model_id="pyannote-community-1",
        )
    assert str(caught.value) == "The configured diarization model could not load"
    assert "private diagnostics" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_adapter_rejects_empty_loader_result(tmp_path: Path):
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    model_source = tmp_path / "community-1"
    model_source.mkdir()
    engine = PyannoteCommunityEngine(
        model_source=model_source,
        cache_dir=tmp_path / "models",
        device="cpu",
        pipeline_loader=lambda _source, _cache: None,
    )

    with pytest.raises(DiarizationError, match="model could not load"):
        await engine.diarize(
            audio_path,
            duration_ms=1000,
            model_id="pyannote-community-1",
        )


@pytest.mark.asyncio
async def test_cuda_initialization_failure_uses_fresh_cpu_pipeline(
    tmp_path: Path,
    monkeypatch,
):
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    model_source = tmp_path / "community-1"
    model_source.mkdir()
    gpu_pipeline = FakePipeline([], move_error=RuntimeError("CUDA unavailable"))
    cpu_pipeline = FakePipeline([(0.0, 1.0, "SPEAKER_00")])
    pipelines = iter([gpu_pipeline, cpu_pipeline])
    empty_cache_calls: list[bool] = []
    fake_torch = SimpleNamespace(
        device=lambda value: value,
        cuda=SimpleNamespace(
            is_available=lambda: True,
            empty_cache=lambda: empty_cache_calls.append(True),
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    engine = PyannoteCommunityEngine(
        model_source=model_source,
        cache_dir=tmp_path / "models",
        device="cuda",
        pipeline_loader=lambda _source, _cache: next(pipelines),
    )

    segments = await engine.diarize(
        audio_path,
        duration_ms=1000,
        model_id="pyannote-community-1",
    )

    assert segments == [DiarizationSegment("SPEAKER_00", 0, 1000)]
    assert gpu_pipeline.devices == ["cuda"]
    assert cpu_pipeline.paths == [str(audio_path)]
    assert engine.capabilities().device == "cpu"
    assert empty_cache_calls == [True]


@pytest.mark.asyncio
async def test_cuda_runtime_failure_retries_once_on_cpu(tmp_path: Path, monkeypatch):
    audio_path = tmp_path / "inference.wav"
    audio_path.write_bytes(b"RIFF-test")
    model_source = tmp_path / "community-1"
    model_source.mkdir()
    gpu_pipeline = FakePipeline([], run_error=RuntimeError("CUDA out of memory"))
    cpu_pipeline = FakePipeline([(0.25, 0.75, "SPEAKER_00")])
    pipelines = iter([gpu_pipeline, cpu_pipeline])
    empty_cache_calls: list[bool] = []
    fake_torch = SimpleNamespace(
        device=lambda value: value,
        cuda=SimpleNamespace(
            is_available=lambda: True,
            empty_cache=lambda: empty_cache_calls.append(True),
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    engine = PyannoteCommunityEngine(
        model_source=model_source,
        cache_dir=tmp_path / "models",
        device="cuda",
        pipeline_loader=lambda _source, _cache: next(pipelines),
    )
    segments = await engine.diarize(
        audio_path,
        duration_ms=1000,
        model_id="pyannote-community-1",
    )

    assert segments == [DiarizationSegment("SPEAKER_00", 250, 750)]
    assert gpu_pipeline.devices == ["cuda"]
    assert engine.capabilities().device == "cpu"
    assert empty_cache_calls == [True]


def test_factory_keeps_optional_runtime_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FINAL_DIARIZATION", False)
    assert build_configured_diarization_engine() is None


def test_factory_copies_config_without_loading_model(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FINAL_DIARIZATION", True)
    monkeypatch.setattr(settings, "FINAL_DIARIZATION_MODEL_SOURCE", Path("local/community-1"))
    monkeypatch.setattr(settings, "DEFAULT_DIARIZATION_MODEL", "configured-model")
    monkeypatch.setattr(settings, "FINAL_DIARIZATION_DEVICE", "cpu")
    monkeypatch.setattr(settings, "FINAL_DIARIZATION_TELEMETRY", False)

    engine = build_configured_diarization_engine()

    assert isinstance(engine, PyannoteCommunityEngine)
    assert engine.model_source == Path("local/community-1")
    assert engine.model_id == "configured-model"
    assert engine.cache_dir == settings.MODELS_DIR / "pyannote"
    assert engine.requested_device == "cpu"
    assert engine.telemetry_enabled is False
    assert engine._pipeline is None
