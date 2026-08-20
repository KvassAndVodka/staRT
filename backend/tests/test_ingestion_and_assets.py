"""
Hermetic Tests for Ingestion Security and Validation
"""
import pytest
import asyncio
import io
import os
import shutil
import socket
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.adapters.ingestion.stream_capture import (
    CapturedFragment,
    IngestionError,
    IngestionSecurityError,
    PcmTimestampTracker,
    ResolvedSource,
    SourceReconnecting,
    StreamDiscontinuity,
    StreamIngestionAdapter,
)
from app.config import settings
from app.adapters.ingestion.ssrf_proxy import (
    OutboundNetworkPolicy,
    PolicyProxy,
    PublicEndpoint,
    filter_forward_headers,
)


def _public_dns(host, port, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

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
    with patch("app.adapters.ingestion.ssrf_proxy.socket.getaddrinfo", side_effect=_public_dns):
        StreamIngestionAdapter.validate_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        StreamIngestionAdapter.validate_url("https://example.com/live_stream.m3u8")


def test_ssrf_rejects_dns_failure_mixed_answers_and_ipv4_mapped_ipv6():
    def failed_dns(*_args, **_kwargs):
        raise socket.gaierror("missing")

    with pytest.raises(IngestionSecurityError, match="DNS resolution failed"):
        OutboundNetworkPolicy(failed_dns).validate_url("https://missing.example/audio")

    def mixed_dns(_host, port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    with pytest.raises(IngestionSecurityError, match="Prohibited IP"):
        OutboundNetworkPolicy(mixed_dns).validate_url("https://mixed.example/audio")

    def mapped_dns(_host, port, **_kwargs):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", port, 0, 0)),
        ]

    with pytest.raises(IngestionSecurityError, match="Prohibited IP"):
        OutboundNetworkPolicy(mapped_dns).validate_url("https://mapped.example/audio")


def test_extractor_credentials_do_not_cross_origins():
    headers = {
        "User-Agent": "test-agent",
        "Cookie": "session=secret",
        "Authorization": "Bearer secret",
        "Referer": "https://media.example/watch/1",
    }
    cross_origin = filter_forward_headers(
        headers.items(),
        "https://media.example/watch/1",
        "https://cdn.example/audio.m4a",
    )
    assert cross_origin == {"User-Agent": "test-agent"}
    assert filter_forward_headers(
        headers.items(),
        "https://media.example/watch/1",
        "https://media.example/audio.m4a",
    ) == headers


def test_master_plan_remuxes_known_codecs_and_uses_lossless_fallback():
    adapter = StreamIngestionAdapter(
        "master-plan",
        "https://example.com/audio",
        network_policy=OutboundNetworkPolicy(_public_dns),
    )
    adapter._configure_master_output("opus")
    assert adapter.master_path.name == "master.mka"
    assert adapter._master_ffmpeg_args() == ["-c:a", "copy", "-f", "matroska"]
    assert adapter.master_audio_transcoded is False

    adapter._configure_master_output("unknown_codec")
    assert adapter.master_path.name == "master.flac"
    assert adapter._master_ffmpeg_args() == [
        "-c:a", "flac", "-compression_level", "8", "-f", "flac",
    ]
    assert adapter.master_operation == "lossless_transcode_fallback"
    assert adapter.master_audio_transcoded is True


@pytest.mark.asyncio
async def test_pcm_timestamp_tracker_splits_exactly_at_pts_discontinuities():
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"#format: frame checksums\n"
        b"#tb 0: 1/16000\n"
        b"0, 100, 100, 3, 6, hash\n"
        b"0, 103, 103, 2, 4, hash\n"
        b"0, 500, 500, 2, 4, hash\n"
        b"0, 7, 7, 3, 6, hash\n"
    )
    reader.feed_eof()

    tracker = PcmTimestampTracker(reader, sample_rate=16000)
    first = await tracker.consume(4)
    second = await tracker.consume(6)
    await tracker.assert_exhausted()

    assert [(span.source_pts_start, span.source_pts_end, span.sample_count) for span in first] == [
        (100, 104, 4),
    ]
    assert [(span.source_pts_start, span.source_pts_end, span.sample_count) for span in second] == [
        (104, 105, 1),
        (500, 502, 2),
        (7, 10, 3),
    ]


@pytest.mark.asyncio
async def test_policy_proxy_blocks_redirect_target_to_loopback():
    async def redirect(_reader, writer):
        writer.write(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://127.0.0.1/private\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(redirect, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    def redirect_test_dns(host, port, **kwargs):
        if host == "127.0.0.1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        return _public_dns(host, port, **kwargs)

    proxy = PolicyProxy(OutboundNetworkPolicy(redirect_test_dns))
    await proxy.start()
    original_resolve = proxy._resolve

    async def resolve_for_test(host, port):
        if host == "public.example":
            return (
                PublicEndpoint(host, port, "127.0.0.1", socket.AddressFamily.AF_INET),
            )
        return await original_resolve(host, port)

    proxy._resolve = resolve_for_test
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", int(proxy.url.rsplit(":", 1)[1]))
        writer.write(
            f"GET http://public.example:{upstream_port}/start HTTP/1.1\r\n"
            f"Host: public.example:{upstream_port}\r\n\r\n".encode()
        )
        await writer.drain()
        first_response = await reader.read()
        writer.close()
        await writer.wait_closed()
        assert b"302 Found" in first_response
        assert b"http://127.0.0.1/private" in first_response

        reader, writer = await asyncio.open_connection("127.0.0.1", int(proxy.url.rsplit(":", 1)[1]))
        writer.write(
            b"GET http://127.0.0.1/private HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n\r\n"
        )
        await writer.drain()
        blocked_response = await reader.read()
        writer.close()
        await writer.wait_closed()
        assert b"403 Blocked" in blocked_response
        assert b"Prohibited IP address range" in blocked_response
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_policy_proxy_removes_hop_by_hop_and_connection_nominated_headers():
    received_request = asyncio.get_running_loop().create_future()

    async def upstream_handler(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        received_request.set_result(request)
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    proxy = PolicyProxy(OutboundNetworkPolicy(_public_dns))
    await proxy.start()

    async def resolve_for_test(host, port):
        return (
            PublicEndpoint(host, port, "127.0.0.1", socket.AddressFamily.AF_INET),
        )

    proxy._resolve = resolve_for_test
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            int(proxy.url.rsplit(":", 1)[1]),
        )
        writer.write(
            f"GET http://public.example:{upstream_port}/audio HTTP/1.1\r\n"
            f"Host: public.example:{upstream_port}\r\n"
            "Connection: keep-alive, X-Remove\r\n"
            "Proxy-Connection: keep-alive\r\n"
            "TE: trailers\r\n"
            "X-Remove: secret\r\n"
            "X-Keep: value\r\n\r\n".encode()
        )
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()
        forwarded = (await received_request).lower()
        assert b"x-keep: value" in forwarded
        assert b"x-remove:" not in forwarded
        assert b"proxy-connection:" not in forwarded
        assert b"te: trailers" not in forwarded
        assert forwarded.count(b"connection:") == 1
        assert b"connection: close" in forwarded
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()

@pytest.mark.asyncio
async def test_invalid_extractor_url_raises_ingestion_error():
    adapter = StreamIngestionAdapter(
        "test-session",
        "https://unsupported-site.com/video",
        network_policy=OutboundNetworkPolicy(_public_dns),
    )
    with patch("app.adapters.ingestion.stream_capture.yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_instance = mock_ydl_cls.return_value.__enter__.return_value
        mock_instance.extract_info.side_effect = Exception("Extractor error")
        try:
            with pytest.raises(IngestionError):
                await adapter.resolve_source()
        finally:
            await adapter.stop()


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

    adapter = StreamIngestionAdapter(
        "stall-session",
        "https://example.com/live.m3u8",
        network_policy=OutboundNetworkPolicy(_public_dns),
    )
    adapter.resolved_source = ResolvedSource(
        title="Stall",
        media_url="https://example.com/live.m3u8",
        is_live=True,
        duration_sec=None,
        http_headers={},
    )
    process = FakeProcess()
    async def spawn_fake_ffmpeg(*args, **_kwargs):
        timing_target = next(
            arg for arg in args if isinstance(arg, str) and arg.startswith("pipe:") and arg != "pipe:1"
        )
        timing_fd = int(timing_target.split(":", 1)[1])
        os.write(
            timing_fd,
            b"#format: frame checksums\n"
            b"#tb 0: 1/16000\n"
            b"0, 0, 0, 1, 2, hash\n"
            b"0, 1, 1, 1, 2, hash\n",
        )
        return process

    with patch(
        "app.adapters.ingestion.stream_capture.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=spawn_fake_ffmpeg,
    ) as create_process:
        with patch.object(settings, "SOURCE_STALL_THRESHOLD_SEC", 0.001):
            events = [
                event async for event in adapter.stream_pcm_fragments(
                    fragment_duration_sec=1 / 16000,
                    sample_rate=16000,
                )
            ]

    command = list(create_process.await_args.args)
    assert ["-c:a", "copy", "-f", "matroska", str(adapter.master_path)] == command[
        command.index("copy") - 1:command.index(str(adapter.master_path)) + 1
    ]
    assert str(adapter.playback_path) in command
    assert command.count("pcm_s16le") == 2
    assert "-copyts" in command
    assert "framehash" in command
    assert create_process.await_args.kwargs["env"]["NO_PROXY"] == ""

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


@pytest.mark.asyncio
async def test_pcm_pts_jump_and_reset_split_publication_at_exact_sample_boundaries():
    class FakeStdout:
        def __init__(self):
            self.done = False

        async def readexactly(self, byte_count):
            if not self.done:
                self.done = True
                return b"\x01\x00" * (byte_count // 2)
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

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def spawn_fake_ffmpeg(*args, **_kwargs):
        timing_target = next(
            arg for arg in args if isinstance(arg, str) and arg.startswith("pipe:") and arg != "pipe:1"
        )
        os.write(
            int(timing_target.split(":", 1)[1]),
            b"#format: frame checksums\n"
            b"#tb 0: 1/1000\n"
            b"0, 0, 0, 2, 4, hash\n"
            b"0, 10, 10, 2, 4, hash\n"
            b"0, 1, 1, 2, 4, hash\n",
        )
        return FakeProcess()

    adapter = StreamIngestionAdapter(
        "pts-boundaries",
        "https://example.com/finite.wav",
        network_policy=OutboundNetworkPolicy(_public_dns),
    )
    adapter.resolved_source = ResolvedSource(
        title="PTS boundaries",
        media_url="https://example.com/finite.wav",
        is_live=False,
        duration_sec=0.006,
        http_headers={},
        container="wav",
        codec="pcm_s16le",
    )
    adapter._configure_master_output("pcm_s16le")

    with patch(
        "app.adapters.ingestion.stream_capture.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=spawn_fake_ffmpeg,
    ):
        events = [
            event async for event in adapter.stream_pcm_fragments(
                fragment_duration_sec=0.006,
                sample_rate=1000,
            )
        ]

    assert [type(event) for event in events] == [
        CapturedFragment,
        StreamDiscontinuity,
        CapturedFragment,
        StreamDiscontinuity,
        CapturedFragment,
    ]
    first, jump, second, reset, third = events
    assert (first.stream_epoch, first.sample_start, first.sample_end) == (0, 0, 2)
    assert (first.source_pts_start, first.source_pts_end) == (0, 2)
    assert jump.reason == "source_dvr_jump"
    assert (jump.source_start_ms, jump.source_end_ms) == (2, 10)
    assert (second.stream_epoch, second.sample_start, second.sample_end) == (1, 0, 2)
    assert (second.source_pts_start, second.source_pts_end) == (10, 12)
    assert reset.reason == "source_pts_reset"
    assert (reset.source_start_ms, reset.source_end_ms) == (12, 12)
    assert (third.stream_epoch, third.sample_start, third.sample_end) == (2, 0, 2)
    assert (third.source_pts_start, third.source_pts_end) == (1, 3)


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg acceptance test requires ffmpeg and ffprobe",
)
async def test_real_ffmpeg_uses_policy_proxy_and_preserves_master_codec():
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x01\x00" * 1600)
    wav_bytes = wav_buffer.getvalue()

    async def serve_wav(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: audio/wav\r\n"
            + f"Content-Length: {len(wav_bytes)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + wav_bytes
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(serve_wav, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    source_url = f"http://public.example:{upstream_port}/tone.wav"
    adapter = StreamIngestionAdapter(
        "real-ffmpeg",
        source_url,
        network_policy=OutboundNetworkPolicy(_public_dns),
    )
    adapter.resolved_source = ResolvedSource(
        title="Tone",
        media_url=source_url,
        is_live=False,
        duration_sec=0.1,
        http_headers={},
        container="wav",
        codec="pcm_s16le",
    )
    adapter._configure_master_output("pcm_s16le")
    original_resolve = adapter._policy_proxy._resolve

    async def resolve_for_test(host, port):
        if host == "public.example":
            return (
                PublicEndpoint(host, port, "127.0.0.1", socket.AddressFamily.AF_INET),
            )
        return await original_resolve(host, port)

    adapter._policy_proxy._resolve = resolve_for_test
    try:
        events = [
            event async for event in adapter.stream_pcm_fragments(
                fragment_duration_sec=0.05,
                sample_rate=16000,
            )
        ]
        assert events
        assert all(isinstance(event, CapturedFragment) for event in events)
        master = await adapter.probe_media_file(adapter.master_path)
        playback = await adapter.probe_media_file(adapter.playback_path)
        inference = await adapter.probe_media_file(adapter.inference_path)
        assert master.container == "matroska"
        assert master.codec == "pcm_s16le"
        assert playback.codec == "aac"
        assert inference.codec == "pcm_s16le"
        assert sum(event.sample_count for event in events) == 1600
    finally:
        await adapter.stop()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg HLS acceptance test requires ffmpeg and ffprobe",
)
async def test_real_hls_discontinuity_becomes_a_pts_epoch_boundary(tmp_path: Path):
    async def make_segment(path: Path, frequency: int, offset_seconds: int) -> None:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency={frequency}:sample_rate=16000:duration=0.2",
        ]
        if offset_seconds:
            command.extend(["-output_ts_offset", str(offset_seconds)])
        command.extend(["-c:a", "aac", "-f", "mpegts", str(path)])
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode(errors="replace")

    await make_segment(tmp_path / "first.ts", 440, 0)
    await make_segment(tmp_path / "second.ts", 880, 5)
    playlist = (
        b"#EXTM3U\n"
        b"#EXT-X-VERSION:3\n"
        b"#EXT-X-TARGETDURATION:1\n"
        b"#EXT-X-MEDIA-SEQUENCE:0\n"
        b"#EXTINF:0.2,\nfirst.ts\n"
        b"#EXT-X-DISCONTINUITY\n"
        b"#EXTINF:0.2,\nsecond.ts\n"
        b"#EXT-X-ENDLIST\n"
    )
    payloads = {
        "/playlist.m3u8": ("application/vnd.apple.mpegurl", playlist),
        "/first.ts": ("video/mp2t", (tmp_path / "first.ts").read_bytes()),
        "/second.ts": ("video/mp2t", (tmp_path / "second.ts").read_bytes()),
    }

    async def serve_hls(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ", 2)[1].decode("ascii")
        content_type, payload = payloads[path]
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Type: {content_type}\r\n".encode()
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(serve_hls, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    source_url = f"http://public.example:{upstream_port}/playlist.m3u8"
    adapter = StreamIngestionAdapter(
        "real-hls-discontinuity",
        source_url,
        network_policy=OutboundNetworkPolicy(_public_dns),
    )
    adapter.resolved_source = ResolvedSource(
        title="Discontinuous HLS",
        media_url=source_url,
        is_live=False,
        duration_sec=0.4,
        http_headers={},
        container="mpegts",
        codec="aac",
    )
    adapter._configure_master_output("aac")

    original_resolve = adapter._policy_proxy._resolve

    async def resolve_for_test(host, port):
        if host == "public.example":
            return (
                PublicEndpoint(host, port, "127.0.0.1", socket.AddressFamily.AF_INET),
            )
        return await original_resolve(host, port)

    adapter._policy_proxy._resolve = resolve_for_test
    try:
        events = [
            event async for event in adapter.stream_pcm_fragments(
                fragment_duration_sec=0.1,
                sample_rate=16000,
            )
        ]
        fragments = [event for event in events if isinstance(event, CapturedFragment)]
        boundaries = [event for event in events if isinstance(event, StreamDiscontinuity)]
        assert fragments
        assert all(fragment.source_pts_start is not None for fragment in fragments)
        assert all(fragment.source_pts_end is not None for fragment in fragments)
        assert any(boundary.reason == "source_dvr_jump" for boundary in boundaries)
        assert sorted({fragment.stream_epoch for fragment in fragments}) == list(
            range(max(fragment.stream_epoch for fragment in fragments) + 1)
        )
        with wave.open(str(adapter.inference_path), "rb") as inference_wav:
            assert sum(fragment.sample_count for fragment in fragments) == (
                inference_wav.getnframes()
            )
    finally:
        await adapter.stop()
        upstream.close()
        await upstream.wait_closed()
