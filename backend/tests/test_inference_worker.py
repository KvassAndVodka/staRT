"""
Contract tests for InferenceWorker thread-shielding, cancellation ownership, and shutdown.
"""
import pytest
import asyncio
import threading
import numpy as np
from unittest.mock import MagicMock

from app.application.inference_worker import InferenceWorker, WorkerClosedError, InferenceBusyTimeout
from app.adapters.asr.faster_whisper_engine import HypothesisWord

@pytest.mark.asyncio
async def test_worker_cancellation_ownership_and_timeout():
    worker = InferenceWorker()
    engine_entered = threading.Event()
    release_engine = threading.Event()

    fake_engine = MagicMock()
    def blocking_transcribe(audio_chunk, window_start_ms, language_mode):
        engine_entered.set()
        release_engine.wait(timeout=5.0)
        return [HypothesisWord(start_ms=window_start_ms, end_ms=window_start_ms + 1000, text="test")]

    fake_engine.transcribe_window.side_effect = blocking_transcribe

    dummy_audio = np.zeros(16000, dtype=np.float32)

    # 1. Start run() as a task
    task = asyncio.create_task(
        worker.run(fake_engine, dummy_audio, window_start_ms=0, language_mode="auto-mixed")
    )

    # Wait until the thread actually entered the engine
    await asyncio.to_thread(engine_entered.wait, 5.0)
    assert engine_entered.is_set()

    # 2. Cancel the caller's asyncio task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 3. Call wait_idle with short timeout; must raise InferenceBusyTimeout because thread is still blocked
    with pytest.raises(InferenceBusyTimeout):
        await worker.wait_idle(timeout=0.05)

    assert worker._current_async_future is not None

    # 4. Release the blocking event
    release_engine.set()

    # 5. wait_idle without timeout must now complete cleanly
    await worker.wait_idle(timeout=None)
    assert worker._current_async_future is None

    # 6. Close the worker and assert WorkerClosedError on subsequent run
    await worker.close()
    assert worker._closed is True
    assert worker.last_terminal_outcome is not None
    assert worker.last_terminal_outcome.status == "succeeded"

    with pytest.raises(WorkerClosedError):
        await worker.run(fake_engine, dummy_audio, window_start_ms=0, language_mode="auto-mixed")

@pytest.mark.asyncio
async def test_worker_cancellation_with_underlying_exception():
    """Verify that if the caller is cancelled and the underlying engine raises an exception, close() shuts down cleanly."""
    worker = InferenceWorker()
    engine_entered = threading.Event()
    release_engine = threading.Event()

    fake_engine = MagicMock()
    def failing_transcribe(audio_chunk, window_start_ms, language_mode):
        engine_entered.set()
        release_engine.wait(timeout=5.0)
        raise RuntimeError("CUDA Out of Memory in Thread")

    fake_engine.transcribe_window.side_effect = failing_transcribe
    dummy_audio = np.zeros(16000, dtype=np.float32)

    task = asyncio.create_task(
        worker.run(fake_engine, dummy_audio, window_start_ms=0, language_mode="auto-mixed")
    )

    await asyncio.to_thread(engine_entered.wait, 5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Release engine to raise the exception in the background thread
    release_engine.set()

    # close() must cleanly wait for the thread and shut down the executor regardless of the exception
    outcome = await worker.close()
    assert worker._closed is True
    assert outcome is not None
    assert outcome.status == "failed"
    assert isinstance(outcome.error, RuntimeError)
    assert "CUDA Out of Memory" in str(outcome.error)
    assert worker.last_terminal_outcome == outcome
