"""
Dedicated single-threaded inference worker with thread-shielding, cancellation ownership,
and explicit idle/shutdown contracts.
"""
import asyncio
import concurrent.futures
from dataclasses import dataclass
from typing import Optional, List
import numpy as np

from app.adapters.asr.faster_whisper_engine import FasterWhisperASREngine, HypothesisWord

class WorkerClosedError(Exception):
    """Raised when work is submitted to a closed InferenceWorker."""
    pass

class InferenceBusyTimeout(Exception):
    """Raised when wait_idle times out while an inference thread is still executing."""
    pass


@dataclass(frozen=True)
class InferenceTerminalOutcome:
    """Observable result of work that outlived its cancelled async caller."""

    status: str
    result: Optional[List[HypothesisWord]] = None
    error: Optional[BaseException] = None


class InferenceWorker:
    def __init__(self, executor: Optional[concurrent.futures.ThreadPoolExecutor] = None):
        self._executor = executor or concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._submit_lock = asyncio.Lock()
        self._current_async_future: Optional[asyncio.Future] = None
        self._current_concurrent_future: Optional[concurrent.futures.Future] = None
        self.last_terminal_outcome: Optional[InferenceTerminalOutcome] = None
        self._closed = False

    async def run(
        self,
        engine: FasterWhisperASREngine,
        audio_chunk: np.ndarray,
        window_start_ms: int,
        language_mode: str
    ) -> List[HypothesisWord]:
        async with self._submit_lock:
            # If a previous call was cancelled at the caller level, ensure the underlying thread finished
            retained = await self.wait_idle(timeout=None)
            if retained and retained.status == "failed":
                assert retained.error is not None
                raise retained.error
            
            if self._closed:
                raise WorkerClosedError("InferenceWorker is closed")

            concurrent_future = self._executor.submit(
                engine.transcribe_window,
                audio_chunk,
                window_start_ms,
                language_mode
            )
            self._current_concurrent_future = concurrent_future
            async_future = asyncio.wrap_future(concurrent_future)
            self._current_async_future = async_future

            try:
                # Shield from caller cancellation so we retain ownership of the executing future
                return await asyncio.shield(async_future)
            finally:
                if async_future.done():
                    self._current_async_future = None
                    self._current_concurrent_future = None

    async def wait_idle(
        self,
        timeout: Optional[float] = None,
    ) -> Optional[InferenceTerminalOutcome]:
        """
        Wait for any active inference thread to complete.
        If timeout is exceeded, raises InferenceBusyTimeout while retaining the future.
        """
        future = self._current_async_future
        if future is None:
            return None

        try:
            if timeout is None:
                result = await asyncio.shield(future)
            else:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            outcome = InferenceTerminalOutcome(status="succeeded", result=result)
        except asyncio.TimeoutError:
            raise InferenceBusyTimeout("Inference thread is still executing")
        except Exception as exc:
            outcome = InferenceTerminalOutcome(status="failed", error=exc)
        finally:
            if future.done():
                self._current_async_future = None
                self._current_concurrent_future = None
        self.last_terminal_outcome = outcome
        return outcome

    async def close(self) -> Optional[InferenceTerminalOutcome]:
        """Prevent new submissions, join active work, and shutdown executor."""
        self._closed = True
        outcome = None
        try:
            outcome = await self.wait_idle(timeout=None)
        finally:
            await asyncio.to_thread(self._executor.shutdown, wait=True)
        return outcome
