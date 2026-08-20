"""
Stream Ingestion Adapter using yt-dlp and FFmpeg.
Features:
- Connection-time SSRF enforcement for every HTTP(S) request and redirect
- Same-origin forwarding for extractor credentials
- Non-blocking continuous stderr draining (prevents pipe deadlock)
- Three outputs: source-faithful master, playback derivative, and inference PCM
- Packet-level PTS tracking over a structured FFmpeg framehash side channel
- Robust subprocess returncode inspection with strict joining
- FFprobe metadata extraction for master audio assets (raises on failure)
"""
import os
import json
import asyncio
import urllib.parse
import wave
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import AsyncGenerator, Optional, Dict, Any, List, Union
from dataclasses import dataclass
import numpy as np
import yt_dlp

from app.adapters.storage.fragment_files import publish_pcm_fragment
from app.adapters.ingestion.ssrf_proxy import (
    IngestionSecurityError,
    OutboundNetworkPolicy,
    PolicyProxy,
    filter_forward_headers,
)

from app.config import settings

class IngestionError(Exception):
    pass

@dataclass
class ResolvedSource:
    title: str
    media_url: str
    is_live: bool
    duration_sec: Optional[float]
    http_headers: Dict[str, str]
    container: Optional[str] = "m4a"
    codec: Optional[str] = "aac"

@dataclass
class ProbedMediaInfo:
    container: str
    codec: str
    channels: int
    sample_rate_hz: int
    duration_ms: int
    size_bytes: int


@dataclass(frozen=True)
class CapturedFragment:
    sequence: int
    stream_epoch: int
    sample_start: int
    sample_end: int
    sample_count: int
    source_start_ms: int
    source_end_ms: int
    wall_started_at: datetime
    wall_ended_at: datetime
    source_pts_start: Optional[int]
    source_pts_end: Optional[int]
    audio: np.ndarray
    path: Path
    sha256: str


@dataclass(frozen=True)
class StreamDiscontinuity:
    previous_stream_epoch: int
    next_stream_epoch: int
    source_start_ms: int
    source_end_ms: int
    wall_started_at: datetime
    wall_ended_at: datetime
    reason: str = "source_stall"
    recoverable: bool = False


@dataclass(frozen=True)
class SourceReconnecting:
    stream_epoch: int
    wall_started_at: datetime
    reason: str = "source_stall"


@dataclass(frozen=True)
class PcmTimingSpan:
    """A byte-aligned interval on FFmpeg's normalized PCM presentation timeline.

    PTS values use the inference stream time base, which the tracker requires to
    be exactly ``1 / sample_rate``. This makes the persisted integer PTS values
    interpretable using each fragment's existing ``sample_rate_hz`` field.
    """

    source_pts_start: int
    source_pts_end: int
    sample_count: int


class PcmTimestampTracker:
    """Match raw PCM bytes to structured packet timestamps from ``framehash``."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        *,
        sample_rate: int,
        bytes_per_sample: int = 2,
    ) -> None:
        self._reader = reader
        self._sample_rate = sample_rate
        self._bytes_per_sample = bytes_per_sample
        self._time_base_seen = False
        self._pending: deque[PcmTimingSpan] = deque()

    async def _read_packet(self) -> PcmTimingSpan:
        while True:
            line = await self._reader.readline()
            if not line:
                raise IngestionError(
                    "FFmpeg timestamp side channel ended before the PCM stream"
                )
            text = line.decode("utf-8", errors="strict").strip()
            if not text:
                continue
            if text.startswith("#tb 0:"):
                time_base = text.split(":", 1)[1].strip()
                if time_base != f"1/{self._sample_rate}":
                    raise IngestionError(
                        "Unexpected inference timestamp time base "
                        f"{time_base}; expected 1/{self._sample_rate}"
                    )
                self._time_base_seen = True
                continue
            if text.startswith("#"):
                continue
            if not self._time_base_seen:
                raise IngestionError("FFmpeg emitted timestamp packets before a time base")

            fields = [field.strip() for field in text.split(",")]
            if len(fields) < 6 or fields[0] != "0":
                raise IngestionError(f"Malformed FFmpeg framehash row: {text[:160]}")
            try:
                pts = int(fields[2])
                duration = int(fields[3])
                size_bytes = int(fields[4])
            except ValueError as exc:
                raise IngestionError(
                    f"Non-integer FFmpeg framehash timing row: {text[:160]}"
                ) from exc
            if duration <= 0 or size_bytes <= 0:
                raise IngestionError("FFmpeg emitted an empty PCM timing packet")
            if size_bytes % self._bytes_per_sample:
                raise IngestionError("FFmpeg emitted a misaligned PCM timing packet")
            sample_count = size_bytes // self._bytes_per_sample
            if duration != sample_count:
                raise IngestionError(
                    "FFmpeg PCM packet duration does not match its sample count"
                )
            return PcmTimingSpan(
                source_pts_start=pts,
                source_pts_end=pts + duration,
                sample_count=sample_count,
            )

    async def consume(self, sample_count: int) -> list[PcmTimingSpan]:
        """Return timing spans that cover exactly the next PCM sample interval."""
        if sample_count <= 0:
            raise ValueError("sample_count must be positive")

        available = sum(span.sample_count for span in self._pending)
        while available < sample_count:
            packet = await self._read_packet()
            self._pending.append(packet)
            available += packet.sample_count

        remaining = sample_count
        result: list[PcmTimingSpan] = []
        while remaining:
            span = self._pending.popleft()
            take = min(remaining, span.sample_count)
            consumed = PcmTimingSpan(
                source_pts_start=span.source_pts_start,
                source_pts_end=span.source_pts_start + take,
                sample_count=take,
            )
            if result and result[-1].source_pts_end == consumed.source_pts_start:
                previous = result[-1]
                result[-1] = PcmTimingSpan(
                    source_pts_start=previous.source_pts_start,
                    source_pts_end=consumed.source_pts_end,
                    sample_count=previous.sample_count + consumed.sample_count,
                )
            else:
                result.append(consumed)

            remaining -= take
            if take < span.sample_count:
                self._pending.appendleft(PcmTimingSpan(
                    source_pts_start=span.source_pts_start + take,
                    source_pts_end=span.source_pts_end,
                    sample_count=span.sample_count - take,
                ))
        return result

    async def assert_exhausted(self) -> None:
        """Fail if the timing stream describes PCM that stdout did not contain."""
        if self._pending:
            raise IngestionError("FFmpeg timestamp side channel exceeded the PCM stream")
        while True:
            line = await self._reader.readline()
            if not line:
                return
            if line.lstrip().startswith(b"#") or not line.strip():
                continue
            raise IngestionError("FFmpeg timestamp side channel exceeded the PCM stream")

class StreamIngestionAdapter:
    _MATROSKA_COPY_CODECS = frozenset({
        "aac", "ac3", "alac", "dts", "eac3", "flac", "mp2", "mp3",
        "opus", "pcm_f32le", "pcm_s16le", "pcm_s24le", "pcm_s32le",
        "truehd", "vorbis",
    })

    def __init__(
        self,
        session_id: str,
        source_url: str,
        *,
        network_policy: Optional[OutboundNetworkPolicy] = None,
    ):
        self.session_id = session_id
        self.source_url = source_url.strip()
        self.session_dir = settings.SESSIONS_DIR / session_id
        self.audio_dir = self.session_dir / "audio"
        self.fragments_dir = self.session_dir / "fragments"
        
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.fragments_dir.mkdir(parents=True, exist_ok=True)
        
        self.master_path = self.audio_dir / "master.mka"
        self.playback_path = self.audio_dir / "playback.m4a"
        self.inference_path = self.audio_dir / "inference.wav"
        self._network_policy = network_policy or OutboundNetworkPolicy()
        self._policy_proxy = PolicyProxy(self._network_policy)
        self.master_operation = "remux"
        self.master_audio_transcoded = False
        self.master_container = "matroska"
        self.master_codec: Optional[str] = None
        
        self.resolved_source: Optional[ResolvedSource] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._was_user_stopped = False
        self._fragment_count = 0
        self._exit_code: Optional[int] = None
        self._stderr_lines: deque = deque(maxlen=30)
        self._stderr_task: Optional[asyncio.Task] = None

    @staticmethod
    def validate_url(url: str) -> None:
        """Reject unsafe schemes, failed DNS, and any non-public DNS answer."""
        OutboundNetworkPolicy().validate_url(url)

    async def _validate_url(self, url: str) -> None:
        parsed = self._network_policy.parse_url(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        await self._network_policy.resolve_host_async(parsed.hostname or "", port)

    async def _ensure_policy_proxy(self) -> None:
        await self._policy_proxy.start()

    def _configure_master_output(self, source_codec: Optional[str]) -> None:
        normalized = (source_codec or "").lower()
        if normalized in self._MATROSKA_COPY_CODECS:
            self.master_path = self.audio_dir / "master.mka"
            self.master_operation = "remux"
            self.master_audio_transcoded = False
            self.master_container = "matroska"
            self.master_codec = normalized
            return
        self.master_path = self.audio_dir / "master.flac"
        self.master_operation = "lossless_transcode_fallback"
        self.master_audio_transcoded = True
        self.master_container = "flac"
        self.master_codec = "flac"

    def _master_ffmpeg_args(self) -> list[str]:
        if self.master_audio_transcoded:
            return ["-c:a", "flac", "-compression_level", "8", "-f", "flac"]
        return ["-c:a", "copy", "-f", "matroska"]

    async def resolve_source(self) -> ResolvedSource:
        """Extract media info using yt-dlp to obtain direct media stream URL."""
        await self._validate_url(self.source_url)
        await self._ensure_policy_proxy()
        
        def _extract():
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                "format": "bestaudio/best",
                "proxy": self._policy_proxy.url,
                "geo_bypass": False,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(self.source_url, download=False)
                
        try:
            info = await asyncio.to_thread(_extract)
        except Exception as e:
            parsed = urllib.parse.urlparse(self.source_url)
            path_lower = parsed.path.lower()
            direct_extensions = (".mp3", ".wav", ".m3u8", ".aac", ".ogg", ".flac", ".m4a", ".mp4")
            if any(path_lower.endswith(ext) for ext in direct_extensions):
                info = {
                    "title": Path(parsed.path).stem or "Direct Audio Stream",
                    "url": self.source_url,
                    "is_live": path_lower.endswith(".m3u8"),
                    "duration": None,
                    "http_headers": {},
                }
            else:
                raise IngestionError(f"Failed to resolve media stream from URL: {str(e)[:150]}")
            
        media_url = info.get("url") or self.source_url
        await self._validate_url(media_url)
        
        is_live = bool(info.get("is_live") or info.get("was_live") or info.get("live_status") == "is_live")
        
        self.resolved_source = ResolvedSource(
            title=info.get("title") or "Live Media Stream",
            media_url=media_url,
            is_live=is_live,
            duration_sec=info.get("duration"),
            http_headers=filter_forward_headers(
                (info.get("http_headers") or {}).items(),
                self.source_url,
                media_url,
            ),
            container=info.get("ext") or "m4a",
            codec=info.get("acodec") or "aac"
        )
        self._configure_master_output(self.resolved_source.codec)
        return self.resolved_source

    async def _drain_stderr(self):
        """Continuously drain stderr in background to prevent FFmpeg pipe buffer deadlock."""
        if not self._process or not self._process.stderr:
            return
        try:
            while not self._process.stderr.at_eof():
                line = await self._process.stderr.readline()
                if not line:
                    break
                self._stderr_lines.append(line.decode("utf-8", errors="replace").strip())
        except Exception:
            pass

    async def stream_pcm_fragments(
        self,
        fragment_duration_sec: float = 2.0,
        sample_rate: int = 16000
    ) -> AsyncGenerator[
        Union[CapturedFragment, SourceReconnecting, StreamDiscontinuity],
        None,
    ]:
        """Capture source-faithful, playback, and timestamped inference audio."""
        if fragment_duration_sec <= 0 or sample_rate <= 0:
            raise ValueError("fragment duration and sample rate must be positive")
        if int(sample_rate * fragment_duration_sec) <= 0:
            raise ValueError("fragment duration is shorter than one sample")
        if not self.resolved_source:
            await self.resolve_source()

        assert self.resolved_source is not None
        target_url = self.resolved_source.media_url
        await self._validate_url(target_url)
        await self._ensure_policy_proxy()

        timing_read_fd, timing_write_fd = os.pipe()
        os.set_inheritable(timing_write_fd, True)
        timing_pipe = os.fdopen(timing_read_fd, "rb", buffering=0)
        timing_transport: Optional[asyncio.ReadTransport] = None

        cmd = ["ffmpeg", "-y", "-copyts"]
        if self.resolved_source.http_headers:
            safe_headers = []
            for k, v in self.resolved_source.http_headers.items():
                safe_headers.append(f"{k}: {v}")
            if safe_headers:
                header_str = "\r\n".join(safe_headers) + "\r\n"
                cmd.extend(["-headers", header_str])

        cmd.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", target_url,
            "-filter_complex",
            (
                f"[0:a:0]aresample={sample_rate},"
                "aformat=sample_fmts=s16:channel_layouts=mono,"
                "asplit=2[inference][timing]"
            ),
            # Output 1: source-faithful remux, or an explicit lossless fallback
            "-map", "0:a:0", "-vn",
        ])
        cmd.extend(self._master_ffmpeg_args())
        cmd.extend([
            str(self.master_path),
            # Output 2: browser-compatible playback derivative
            "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "192k", str(self.playback_path),
            # Output 3: 16kHz mono raw PCM for streaming inference
            "-map", "[inference]", "-c:a", "pcm_s16le",
            "-f", "s16le",
            "pipe:1",
            # Output 4: structured packet PTS for the exact same filtered PCM
            "-map", "[timing]", "-c:a", "pcm_s16le",
            "-flush_packets", "1", "-f", "framehash",
            f"pipe:{timing_write_fd}",
        ])

        process_env = os.environ.copy()
        process_env.update({
            "http_proxy": self._policy_proxy.url,
            "https_proxy": self._policy_proxy.url,
            "HTTP_PROXY": self._policy_proxy.url,
            "HTTPS_PROXY": self._policy_proxy.url,
            "no_proxy": "",
            "NO_PROXY": "",
        })
        cmd[1:1] = ["-http_proxy", self._policy_proxy.url]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=process_env,
                pass_fds=(timing_write_fd,),
            )
        except BaseException:
            timing_pipe.close()
            raise
        finally:
            os.close(timing_write_fd)

        loop = asyncio.get_running_loop()
        timing_reader = asyncio.StreamReader()
        timing_protocol = asyncio.StreamReaderProtocol(timing_reader)
        timing_transport, _ = await loop.connect_read_pipe(
            lambda: timing_protocol,
            timing_pipe,
        )
        timestamp_tracker = PcmTimestampTracker(
            timing_reader,
            sample_rate=sample_rate,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        bytes_per_sample = 2
        chunk_samples = int(sample_rate * fragment_duration_sec)
        chunk_bytes = chunk_samples * bytes_per_sample
        expected_fragment_duration_sec = chunk_samples / sample_rate

        sequence = 0
        stream_epoch = 0
        current_sample_start = 0
        epoch_source_offset_ms = 0
        previous_source_pts_end: Optional[int] = None
        self._fragment_count = 0
        self._was_user_stopped = False

        wav_file = wave.open(str(self.inference_path), "wb")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        
        capture_failed = False
        try:
            while not self._was_user_stopped and self._process.stdout is not None:
                read_started_mono = loop.time()
                read_started_wall = datetime.now(timezone.utc)
                read_task = asyncio.create_task(
                    self._process.stdout.readexactly(chunk_bytes)
                )
                reached_eof = False
                if self.resolved_source.is_live and self._fragment_count > 0:
                    try:
                        data = await asyncio.wait_for(
                            asyncio.shield(read_task),
                            timeout=(
                                expected_fragment_duration_sec
                                + settings.SOURCE_STALL_THRESHOLD_SEC
                            ),
                        )
                    except asyncio.TimeoutError:
                        yield SourceReconnecting(
                            stream_epoch=stream_epoch,
                            wall_started_at=(
                                read_started_wall
                                + timedelta(seconds=expected_fragment_duration_sec)
                            ),
                        )
                        try:
                            data = await read_task
                        except asyncio.IncompleteReadError as exc:
                            data = exc.partial
                            reached_eof = True
                    except asyncio.IncompleteReadError as exc:
                        data = exc.partial
                        reached_eof = True
                else:
                    try:
                        data = await read_task
                    except asyncio.IncompleteReadError as exc:
                        data = exc.partial
                        reached_eof = True
                if not data:
                    await timestamp_tracker.assert_exhausted()
                    break
                if len(data) % bytes_per_sample:
                    raise IngestionError(
                        f"FFmpeg returned misaligned PCM data of {len(data)} bytes"
                    )

                total_sample_count = len(data) // bytes_per_sample
                timing_spans = await timestamp_tracker.consume(total_sample_count)
                chunk_duration_sec = total_sample_count / sample_rate
                read_elapsed_sec = loop.time() - read_started_mono
                wall_ended_at = datetime.now(timezone.utc)
                chunk_wall_started_at = wall_ended_at - timedelta(
                    seconds=chunk_duration_sec
                )
                if (
                    self.resolved_source.is_live
                    and self._fragment_count > 0
                    and read_elapsed_sec - chunk_duration_sec
                    >= settings.SOURCE_STALL_THRESHOLD_SEC
                ):
                    unavailable_ms = max(
                        1,
                        int(round((read_elapsed_sec - chunk_duration_sec) * 1000)),
                    )
                    gap_start_ms = epoch_source_offset_ms + int(
                        current_sample_start * 1000 / sample_rate
                    )
                    gap_end_ms = gap_start_ms + unavailable_ms
                    previous_epoch = stream_epoch
                    stream_epoch += 1
                    sequence = 0
                    current_sample_start = 0
                    epoch_source_offset_ms = gap_end_ms
                    yield StreamDiscontinuity(
                        previous_stream_epoch=previous_epoch,
                        next_stream_epoch=stream_epoch,
                        source_start_ms=gap_start_ms,
                        source_end_ms=gap_end_ms,
                        wall_started_at=read_started_wall + timedelta(seconds=chunk_duration_sec),
                        wall_ended_at=wall_ended_at,
                    )
                    previous_source_pts_end = None

                byte_offset = 0
                wall_offset_samples = 0
                for timing in timing_spans:
                    segment_bytes = timing.sample_count * bytes_per_sample
                    segment_data = data[byte_offset:byte_offset + segment_bytes]
                    byte_offset += segment_bytes

                    segment_wall_started_at = chunk_wall_started_at + timedelta(
                        seconds=wall_offset_samples / sample_rate
                    )
                    wall_offset_samples += timing.sample_count
                    segment_wall_ended_at = chunk_wall_started_at + timedelta(
                        seconds=wall_offset_samples / sample_rate
                    )

                    if (
                        previous_source_pts_end is not None
                        and timing.source_pts_start != previous_source_pts_end
                    ):
                        pts_delta = timing.source_pts_start - previous_source_pts_end
                        gap_start_ms = epoch_source_offset_ms + int(
                            current_sample_start * 1000 / sample_rate
                        )
                        gap_duration_ms = (
                            (pts_delta * 1000 + sample_rate - 1) // sample_rate
                            if pts_delta > 0
                            else 0
                        )
                        gap_end_ms = gap_start_ms + gap_duration_ms
                        previous_epoch = stream_epoch
                        stream_epoch += 1
                        sequence = 0
                        current_sample_start = 0
                        epoch_source_offset_ms = gap_end_ms
                        yield StreamDiscontinuity(
                            previous_stream_epoch=previous_epoch,
                            next_stream_epoch=stream_epoch,
                            source_start_ms=gap_start_ms,
                            source_end_ms=gap_end_ms,
                            wall_started_at=segment_wall_started_at,
                            wall_ended_at=segment_wall_started_at,
                            reason=(
                                "source_dvr_jump"
                                if pts_delta > 0
                                else "source_pts_reset"
                            ),
                        )

                    sample_start = current_sample_start
                    sample_end = sample_start + timing.sample_count
                    current_sample_start = sample_end
                    start_ms = epoch_source_offset_ms + int(
                        sample_start * 1000 / sample_rate
                    )
                    end_ms = epoch_source_offset_ms + int(
                        sample_end * 1000 / sample_rate
                    )
                    frag_filename = (
                        f"epoch_{stream_epoch:04d}_frag_{sequence:06d}_"
                        f"{sample_start}_{sample_end}.raw"
                    )
                    frag_path = self.fragments_dir / frag_filename
                    sha256_hash = publish_pcm_fragment(frag_path, segment_data)
                    wav_file.writeframes(segment_data)
                    audio_int16 = np.frombuffer(segment_data, dtype=np.int16)

                    self._fragment_count += 1
                    yield CapturedFragment(
                        sequence=sequence,
                        stream_epoch=stream_epoch,
                        sample_start=sample_start,
                        sample_end=sample_end,
                        sample_count=timing.sample_count,
                        source_start_ms=start_ms,
                        source_end_ms=end_ms,
                        wall_started_at=segment_wall_started_at,
                        wall_ended_at=segment_wall_ended_at,
                        source_pts_start=timing.source_pts_start,
                        source_pts_end=timing.source_pts_end,
                        audio=audio_int16.astype(np.float32) / 32768.0,
                        path=frag_path,
                        sha256=sha256_hash,
                    )
                    sequence += 1
                    previous_source_pts_end = timing.source_pts_end

                if reached_eof:
                    await timestamp_tracker.assert_exhausted()
                    break
        except BaseException:
            capture_failed = True
            raise
        finally:
            wav_file.close()
            if timing_transport is not None:
                timing_transport.close()

            if self._stderr_task:
                try:
                    await asyncio.wait_for(self._stderr_task, timeout=1.0)
                except Exception:
                    self._stderr_task.cancel()
                    await asyncio.gather(self._stderr_task, return_exceptions=True)

            was_stopped = self._was_user_stopped
            if self._process is not None:
                try:
                    self._exit_code = await asyncio.wait_for(self._process.wait(), timeout=1.5)
                except Exception:
                    try:
                        self._process.kill()
                        self._exit_code = await self._process.wait()
                    except Exception:
                        pass
                self._process = None

            await self._policy_proxy.close()

            if not was_stopped and not capture_failed:
                if self._fragment_count == 0:
                    err_msg = " | ".join(list(self._stderr_lines)[-5:]) if self._stderr_lines else "No output stream"
                    raise IngestionError(f"No audio stream received from source: {err_msg}")
                elif self._exit_code is None:
                    raise IngestionError("Unable to confirm that FFmpeg terminated successfully")
                elif self._exit_code is not None and self._exit_code != 0:
                    err_msg = " | ".join(list(self._stderr_lines)[-5:]) if self._stderr_lines else f"Exit code {self._exit_code}"
                    raise IngestionError(f"FFmpeg process terminated abnormally: {err_msg}")

    async def probe_media_file(self, file_path: Path) -> ProbedMediaInfo:
        """Probe actual container, codec, channels, sample rate, and duration with ffprobe."""
        if not file_path.exists() or file_path.stat().st_size == 0:
            raise IngestionError(f"Media file {file_path.name} does not exist or is empty")
            
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(file_path)
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()
            data = json.loads(stdout.decode("utf-8"))
            
            stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
            fmt = data.get("format", {})
            
            if not stream:
                raise IngestionError(f"No audio streams found in {file_path.name}")
                
            container = fmt.get("format_name", file_path.suffix.replace(".", "")).split(",")[0]
            codec = stream.get("codec_name", "unknown")
            channels = int(stream.get("channels", 1))
            sample_rate = int(stream.get("sample_rate", 16000))
            duration_sec = float(fmt.get("duration", 0.0))
            size_bytes = int(fmt.get("size", file_path.stat().st_size))
            
            return ProbedMediaInfo(
                container=container,
                codec=codec,
                channels=channels,
                sample_rate_hz=sample_rate,
                duration_ms=int(duration_sec * 1000),
                size_bytes=size_bytes
            )
        except IngestionError:
            raise
        except Exception as e:
            raise IngestionError(f"FFprobe failed to inspect {file_path.name}: {e}") from e

    async def stop(self):
        """Cleanly terminate FFmpeg subprocess with strict wait."""
        self._was_user_stopped = True
        if self._process is not None:
            try:
                if self._process.returncode is None:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=1.5)
            except Exception:
                try:
                    self._process.kill()
                    await self._process.wait()
                except Exception:
                    pass
            self._process = None
        await self._policy_proxy.close()
