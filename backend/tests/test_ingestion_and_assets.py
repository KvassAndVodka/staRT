"""
Hermetic Tests for Ingestion Security and Validation
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from app.adapters.ingestion.stream_capture import (
    CapturedFragment,
    IngestionError,
    IngestionSecurityError,
    ResolvedSource,
    SourceReconnecting,
    StreamDiscontinuity,
    StreamIngestionAdapter,
)
from app.config import settings

def test_ssrf_validation_blocks_private_and_local():
    # Loopback
    with pytest.raises(IngestionSecurityError):
        StreamIngestionAdapter.validate_url("http://127.0.0.1/audio.mp3")
    with pytest.raises(IngestionSecurityError):
        StreamIngestionAdapter.validate_url("http://localhost:8080/stream")
        
    # Unsafe schemes
    with pytest.raises(IngestionSecurityError):
        StreamIngestionAdapter.validate_url("file:///etc/passwd")
    with pytest.raises(IngestionSecurityError):
        StreamIngestionAdapter.validate_url("ftp://example.com/audio.wav")

    # Valid public schemes
    StreamIngestionAdapter.validate_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    StreamIngestionAdapter.validate_url("https://example.com/live_stream.m3u8")

@pytest.mark.asyncio
async def test_invalid_extractor_url_raises_ingestion_error():
    adapter = StreamIngestionAdapter("test-session", "https://unsupported-site.com/video")
    with patch("app.adapters.ingestion.stream_capture.yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_instance = mock_ydl_cls.return_value.__enter__.return_value
        mock_instance.extract_info.side_effect = Exception("Extractor error")
        with pytest.raises(IngestionError):
            await adapter.resolve_source()


@pytest.mark.asyncio
async def test_live_pcm_stall_emits_new_epoch_and_gap_event():
    class FakeStdout:
        def __init__(self):
            self.read_count = 0

        async def readexactly(self, byte_count):
            self.read_count += 1
            if self.read_count <= 2:
                await asyncio.sleep(0.01)
                return b"\x01\x00"
            raise asyncio.IncompleteReadError(partial=b"", expected=byte_count)

    class FakeStderr:
        def at_eof(self):
            return True

        async def readline(self):
            return b""

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = 0

        async def wait(self):
            return 0

        def kill(self):
            self.returncode = -9

    adapter = StreamIngestionAdapter("stall-session", "https://example.com/live.m3u8")
    adapter.resolved_source = ResolvedSource(
        title="Stall",
        media_url="https://example.com/live.m3u8",
        is_live=True,
        duration_sec=None,
        http_headers={},
    )
    process = FakeProcess()
    with patch(
        "app.adapters.ingestion.stream_capture.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ):
        with patch.object(settings, "SOURCE_STALL_THRESHOLD_SEC", 0.001):
            events = [
                event async for event in adapter.stream_pcm_fragments(
                    fragment_duration_sec=1 / 16000,
                    sample_rate=16000,
                )
            ]

    assert [type(event) for event in events] == [
        CapturedFragment,
        SourceReconnecting,
        StreamDiscontinuity,
        CapturedFragment,
    ]
    first, reconnecting, gap, second = events
    assert (first.stream_epoch, first.sequence, first.sample_start, first.sample_end) == (0, 0, 0, 1)
    assert (second.stream_epoch, second.sequence, second.sample_start, second.sample_end) == (1, 0, 0, 1)
    assert gap.previous_stream_epoch == 0
    assert gap.next_stream_epoch == 1
    assert gap.source_end_ms > gap.source_start_ms
    assert reconnecting.stream_epoch == 0
