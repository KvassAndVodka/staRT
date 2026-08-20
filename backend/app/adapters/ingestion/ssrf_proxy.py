"""Connection-time SSRF policy and a loopback-only enforcing HTTP proxy."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


class IngestionSecurityError(Exception):
    """The requested network destination violates the ingestion policy."""


@dataclass(frozen=True)
class PublicEndpoint:
    host: str
    port: int
    ip: str
    family: socket.AddressFamily


Resolver = Callable[..., Sequence[tuple]]


class OutboundNetworkPolicy:
    """Resolve every address and reject a host if any answer is not public."""

    allowed_schemes = frozenset({"http", "https"})

    def __init__(self, resolver: Resolver | None = None):
        self._resolver = resolver or socket.getaddrinfo

    @staticmethod
    def _public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise IngestionSecurityError(f"Prohibited IP address range: {value}")
        return address

    @staticmethod
    def parse_url(url: str) -> urllib.parse.SplitResult:
        if not url:
            raise IngestionSecurityError("Empty URL provided")
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise IngestionSecurityError("Invalid URL authority") from exc
        if parsed.scheme.lower() not in OutboundNetworkPolicy.allowed_schemes:
            raise IngestionSecurityError(f"Unsupported URL scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise IngestionSecurityError("Invalid URL: missing hostname")
        if parsed.username is not None or parsed.password is not None:
            raise IngestionSecurityError("Credentials in source URLs are prohibited")
        if port is not None and not 1 <= port <= 65535:
            raise IngestionSecurityError("Invalid URL port")
        return parsed

    def resolve_host(self, host: str, port: int) -> tuple[PublicEndpoint, ...]:
        try:
            records = self._resolver(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except (socket.gaierror, OSError) as exc:
            raise IngestionSecurityError(f"DNS resolution failed for {host}") from exc
        if not records:
            raise IngestionSecurityError(f"DNS resolution returned no addresses for {host}")

        endpoints: list[PublicEndpoint] = []
        seen: set[tuple[int, str]] = set()
        for family, socktype, _proto, _canonname, sockaddr in records:
            if socktype not in (0, socket.SOCK_STREAM):
                continue
            ip_text = str(sockaddr[0])
            self._public_ip(ip_text)
            key = (int(family), ip_text)
            if key in seen:
                continue
            seen.add(key)
            endpoints.append(
                PublicEndpoint(
                    host=host,
                    port=port,
                    ip=ip_text,
                    family=socket.AddressFamily(family),
                )
            )
        if not endpoints:
            raise IngestionSecurityError(f"DNS resolution returned no TCP addresses for {host}")
        return tuple(endpoints)

    def validate_url(self, url: str) -> tuple[PublicEndpoint, ...]:
        parsed = self.parse_url(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        return self.resolve_host(parsed.hostname or "", port)

    async def resolve_host_async(self, host: str, port: int) -> tuple[PublicEndpoint, ...]:
        return await asyncio.to_thread(self.resolve_host, host, port)


class PolicyProxy:
    """Proxy all HTTP(S) connects through validated, DNS-pinned endpoints."""

    max_header_bytes = 64 * 1024

    def __init__(self, policy: OutboundNetworkPolicy | None = None):
        self.policy = policy or OutboundNetworkPolicy()
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def url(self) -> str:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("Policy proxy has not started")
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(
                self._accept_client,
                host="127.0.0.1",
                port=0,
                limit=self.max_header_bytes + 1,
            )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._handle_client(reader, writer)
        finally:
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _resolve(self, host: str, port: int) -> tuple[PublicEndpoint, ...]:
        return await self.policy.resolve_host_async(host, port)

    async def _open_pinned(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        endpoints = await self._resolve(host, port)
        last_error: OSError | None = None
        for endpoint in endpoints:
            try:
                return await asyncio.open_connection(
                    endpoint.ip,
                    endpoint.port,
                    family=endpoint.family,
                )
            except OSError as exc:
                last_error = exc
        raise OSError(f"Could not connect to an approved address for {host}") from last_error

    @staticmethod
    def _authority(value: str, default_port: int) -> tuple[str, int]:
        try:
            parsed = urllib.parse.urlsplit(f"//{value}")
            port = parsed.port or default_port
        except ValueError as exc:
            raise IngestionSecurityError("Invalid proxy request authority") from exc
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise IngestionSecurityError("Invalid proxy request authority")
        if not 1 <= port <= 65535:
            raise IngestionSecurityError("Invalid proxy request port")
        return parsed.hostname, port

    @staticmethod
    def _parse_headers(data: bytes) -> tuple[str, str, str, list[tuple[str, str]]]:
        try:
            text = data.decode("iso-8859-1")
            lines = text[:-4].split("\r\n")
            method, target, version = lines[0].split(" ", 2)
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise IngestionSecurityError("Malformed proxy request") from exc
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise IngestionSecurityError("Unsupported proxy HTTP version")
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line or ":" not in line:
                raise IngestionSecurityError("Malformed proxy request header")
            name, value = line.split(":", 1)
            headers.append((name.strip(), value.strip()))
        return method.upper(), target, version, headers

    @staticmethod
    async def _write_error(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        safe_message = message.replace("\r", " ").replace("\n", " ")[:200]
        body = safe_message.encode("utf-8", errors="replace")
        writer.write(
            f"HTTP/1.1 {status} Blocked\r\nConnection: close\r\n"
            f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    @staticmethod
    async def _relay(
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
    ) -> None:
        while data := await source.read(64 * 1024):
            destination.write(data)
            await destination.drain()

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        tasks = {
            asyncio.create_task(self._relay(client_reader, upstream_writer)),
            asyncio.create_task(self._relay(upstream_reader, client_writer)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
        upstream_writer.close()
        await upstream_writer.wait_closed()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            if len(raw_headers) > self.max_header_bytes:
                raise IngestionSecurityError("Proxy request headers are too large")
            method, target, version, headers = self._parse_headers(raw_headers)

            if method == "CONNECT":
                host, port = self._authority(target, 443)
                upstream_reader, upstream_writer = await self._open_pinned(host, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer)
                return

            parsed = self.policy.parse_url(target)
            if parsed.scheme.lower() != "http":
                raise IngestionSecurityError("HTTPS proxy requests must use CONNECT")
            host = parsed.hostname or ""
            port = parsed.port or 80
            host_headers = [value for name, value in headers if name.lower() == "host"]
            if len(host_headers) != 1:
                raise IngestionSecurityError("Proxy request must contain one Host header")
            header_host, header_port = self._authority(host_headers[0], 80)
            if header_host.lower().rstrip(".") != host.lower().rstrip(".") or header_port != port:
                raise IngestionSecurityError("Proxy request target and Host header differ")

            upstream_reader, upstream_writer = await self._open_pinned(host, port)
            origin_target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            forwarded = [f"{method} {origin_target} {version}\r\n"]
            connection_tokens = {
                token.strip().lower()
                for name, value in headers
                if name.lower() == "connection"
                for token in value.split(",")
                if token.strip()
            }
            hop_by_hop = {
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "proxy-connection",
                "te",
                "trailer",
                "transfer-encoding",
                "upgrade",
                *connection_tokens,
            }
            for name, value in headers:
                if name.lower() in hop_by_hop:
                    continue
                forwarded.append(f"{name}: {value}\r\n")
            forwarded.append("Connection: close\r\n\r\n")
            upstream_writer.write("".join(forwarded).encode("iso-8859-1"))
            await upstream_writer.drain()
            await self._tunnel(reader, writer, upstream_reader, upstream_writer)
        except asyncio.IncompleteReadError:
            return
        except IngestionSecurityError as exc:
            await self._write_error(writer, 403, str(exc))
        except (OSError, asyncio.TimeoutError) as exc:
            await self._write_error(writer, 502, f"Upstream connection failed: {exc}")


def same_origin(left: str, right: str) -> bool:
    try:
        a = OutboundNetworkPolicy.parse_url(left)
        b = OutboundNetworkPolicy.parse_url(right)
    except IngestionSecurityError:
        return False
    a_port = a.port or (443 if a.scheme.lower() == "https" else 80)
    b_port = b.port or (443 if b.scheme.lower() == "https" else 80)
    return (
        a.scheme.lower(),
        (a.hostname or "").lower().rstrip("."),
        a_port,
    ) == (
        b.scheme.lower(),
        (b.hostname or "").lower().rstrip("."),
        b_port,
    )


def filter_forward_headers(
    headers: Iterable[tuple[str, str]],
    source_url: str,
    target_url: str,
) -> dict[str, str]:
    """Keep non-sensitive headers and bind credentials to one origin."""
    same = same_origin(source_url, target_url)
    always = {"user-agent", "accept", "accept-language"}
    same_origin_only = {"authorization", "cookie", "origin", "referer"}
    filtered: dict[str, str] = {}
    for name, value in headers:
        normalized = name.strip().lower()
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            continue
        if normalized in always or (same and normalized in same_origin_only):
            filtered[name.strip()] = value.strip()
    return filtered
