"""
Stream Ingestion Adapter using yt-dlp and FFmpeg.
Features:
- Strict SSRF security policy and URL validation (HTTP, HTTPS, RTMP, RTMPS)
- Forwarding of safe extractor headers (User-Agent, Referer, Cookie, Authorization)
- Non-blocking continuous stderr draining (prevents pipe deadlock)
- Dual output: Master audio + Normalized 16kHz mono inference PCM
- Robust subprocess returncode inspection with strict joining
- FFprobe metadata extraction for master audio assets (raises on failure)
"""
import os
import json
import asyncio
import ipaddress
import urllib.parse
import socket
import wave
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import AsyncGenerator, Optional, Dict, Any, List, Union
from dataclasses import dataclass
import numpy as np
import yt_dlp

from app.adapters.storage.fragment_files import publish_pcm_fragment

from app.config import settings

class IngestionSecurityError(Exception):
    pass

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

class StreamIngestionAdapter:
    def __init__(self, session_id: str, source_url: str):
        self.session_id = session_id
        self.source_url = source_url.strip()
        self.session_dir = settings.SESSIONS_DIR / session_id
        self.audio_dir = self.session_dir / "audio"
        self.fragments_dir = self.session_dir / "fragments"
        
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.fragments_dir.mkdir(parents=True, exist_ok=True)
        
        self.master_path = self.audio_dir / "master.m4a"
        self.inference_path = self.audio_dir / "inference.wav"
        
        self.resolved_source: Optional[ResolvedSource] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._was_user_stopped = False
        self._fragment_count = 0
        self._exit_code: Optional[int] = None
        self._stderr_lines: deque = deque(maxlen=30)
        self._stderr_task: Optional[asyncio.Task] = None

    @staticmethod
    def validate_url(url: str) -> None:
        """Reject non-http/https/rtmp, loopback, private IP, and unsafe schemes."""
        if not url:
            raise IngestionSecurityError("Empty URL provided")
            
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https", "rtmp", "rtmps"):
            raise IngestionSecurityError(f"Unsupported URL scheme: {parsed.scheme}")
        
        hostname = parsed.hostname
        if not hostname:
            raise IngestionSecurityError("Invalid URL: missing hostname")
            
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            raise IngestionSecurityError("Access to localhost is prohibited")
            
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for info in addr_info:
                ip_str = info[4][0]
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                    raise IngestionSecurityError(f"Prohibited IP address range: {ip_str}")
        except socket.gaierror:
            pass

    async def resolve_source(self) -> ResolvedSource:
        """Extract media info using yt-dlp to obtain direct media stream URL."""
        self.validate_url(self.source_url)
        
        def _extract():
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                "format": "bestaudio/best",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(self.source_url, download=False)
                
        try:
            info = await asyncio.to_thread(_extract)
        except Exception as e:
            parsed = urllib.parse.urlparse(self.source_url)
            path_lower = parsed.path.lower()
            direct_extensions = (".mp3", ".wav", ".m3u8", ".aac", ".ogg", ".flac", ".m4a", ".mp4")
            if any(path_lower.endswith(ext) for ext in direct_extensions) or parsed.scheme in ("rtmp", "rtmps"):
                info = {
                    "title": Path(parsed.path).stem or "Direct Audio Stream",
                    "url": self.source_url,
                    "is_live": parsed.scheme in ("rtmp", "rtmps") or path_lower.endswith(".m3u8"),
                    "duration": None,
                    "http_headers": {},
                }
            else:
                raise IngestionError(f"Failed to resolve media stream from URL: {str(e)[:150]}")
            
        media_url = info.get("url") or self.source_url
        self.validate_url(media_url)
        
        is_live = bool(info.get("is_live") or info.get("was_live") or info.get("live_status") == "is_live")
        
        self.resolved_source = ResolvedSource(
            title=info.get("title") or "Live Media Stream",
            media_url=media_url,
            is_live=is_live,
            duration_sec=info.get("duration"),
            http_headers=info.get("http_headers") or {},
            container=info.get("ext") or "m4a",
            codec=info.get("acodec") or "aac"
        )
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
        """
        Runs FFmpeg to simultaneously write master audio and stream 16kHz mono PCM fragments.
        Yields: (sequence, start_ms, end_ms, audio_float32_array, fragment_path, sha256_hash)
        """
        if not self.resolved_source:
            await self.resolve_source()
            
        assert self.resolved_source is not None
        target_url = self.resolved_source.media_url
        self.validate_url(target_url)
        
        cmd = ["ffmpeg", "-y"]
        if self.resolved_source.http_headers:
            safe_headers = []
            for k, v in self.resolved_source.http_headers.items():
                if k.lower() in ("user-agent", "referer", "cookie", "authorization"):
                    safe_headers.append(f"{k}: {v}")
            if safe_headers:
                header_str = "\r\n".join(safe_headers) + "\r\n"
                cmd.extend(["-headers", header_str])

        cmd.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", target_url,
            "-vn",
            # Output 1: Master audio copy or high quality AAC
            "-c:a", "aac", "-b:a", "192k", str(self.master_path),
            # Output 2: 16kHz mono raw PCM for streaming inference
            "-vn",
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", str(sample_rate),
            "-f", "s16le",
            "pipe:1"
        ])
        
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
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
        self._fragment_count = 0
        self._was_user_stopped = False
        loop = asyncio.get_running_loop()

        wav_file = wave.open(str(self.inference_path), "wb")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        
        try:
            while not self._was_user_stopped and self._process.stdout is not None:
                read_started_mono = loop.time()
                read_started_wall = datetime.now(timezone.utc)
                read_task = asyncio.create_task(
                    self._process.stdout.readexactly(chunk_bytes)
                )
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
                        data = await read_task
                else:
                    data = await read_task
                if not data:
                    break

                audio_int16 = np.frombuffer(data, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                sample_count = len(audio_int16)
                fragment_duration_sec = sample_count / sample_rate
                read_elapsed_sec = loop.time() - read_started_mono
                wall_ended_at = datetime.now(timezone.utc)
                if (
                    self.resolved_source.is_live
                    and self._fragment_count > 0
                    and read_elapsed_sec - fragment_duration_sec
                    >= settings.SOURCE_STALL_THRESHOLD_SEC
                ):
                    unavailable_ms = max(
                        1,
                        int(round((read_elapsed_sec - fragment_duration_sec) * 1000)),
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
                        wall_started_at=read_started_wall + timedelta(seconds=fragment_duration_sec),
                        wall_ended_at=wall_ended_at,
                    )

                sample_start = current_sample_start
                sample_end = sample_start + sample_count
                current_sample_start = sample_end

                start_ms = epoch_source_offset_ms + int(sample_start * 1000 / sample_rate)
                end_ms = epoch_source_offset_ms + int(sample_end * 1000 / sample_rate)
                wall_started_at = wall_ended_at - timedelta(seconds=fragment_duration_sec)

                frag_filename = (
                    f"epoch_{stream_epoch:04d}_frag_{sequence:06d}_"
                    f"{sample_start}_{sample_end}.raw"
                )
                frag_path = self.fragments_dir / frag_filename
                sha256_hash = publish_pcm_fragment(frag_path, data)
                wav_file.writeframes(data)

                self._fragment_count += 1
                yield CapturedFragment(
                    sequence=sequence,
                    stream_epoch=stream_epoch,
                    sample_start=sample_start,
                    sample_end=sample_end,
                    sample_count=sample_count,
                    source_start_ms=start_ms,
                    source_end_ms=end_ms,
                    wall_started_at=wall_started_at,
                    wall_ended_at=wall_ended_at,
                    source_pts_start=None,
                    source_pts_end=None,
                    audio=audio_float32,
                    path=frag_path,
                    sha256=sha256_hash,
                )
                sequence += 1
                
        except asyncio.IncompleteReadError as e:
            if e.partial and len(e.partial) > 0:
                if len(e.partial) % bytes_per_sample != 0:
                    raise IngestionError(
                        f"FFmpeg returned a misaligned PCM tail of {len(e.partial)} bytes"
                    )
                audio_int16 = np.frombuffer(e.partial, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                
                sample_count = len(audio_int16)
                sample_start = current_sample_start
                sample_end = sample_start + sample_count
                
                start_ms = epoch_source_offset_ms + int(sample_start * 1000 / sample_rate)
                end_ms = epoch_source_offset_ms + int(sample_end * 1000 / sample_rate)
                wall_ended_at = datetime.now(timezone.utc)
                wall_started_at = wall_ended_at - timedelta(seconds=sample_count / sample_rate)
                
                frag_filename = (
                    f"epoch_{stream_epoch:04d}_frag_{sequence:06d}_"
                    f"{sample_start}_{sample_end}.raw"
                )
                frag_path = self.fragments_dir / frag_filename
                sha256_hash = publish_pcm_fragment(frag_path, e.partial)
                wav_file.writeframes(e.partial)
                self._fragment_count += 1
                yield CapturedFragment(
                    sequence=sequence,
                    stream_epoch=stream_epoch,
                    sample_start=sample_start,
                    sample_end=sample_end,
                    sample_count=sample_count,
                    source_start_ms=start_ms,
                    source_end_ms=end_ms,
                    wall_started_at=wall_started_at,
                    wall_ended_at=wall_ended_at,
                    source_pts_start=None,
                    source_pts_end=None,
                    audio=audio_float32,
                    path=frag_path,
                    sha256=sha256_hash,
                )
        finally:
            wav_file.close()
            
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

            if not was_stopped:
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
