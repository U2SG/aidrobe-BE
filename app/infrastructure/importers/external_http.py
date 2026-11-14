"""Bounded HTTP reads for user-supplied image and product links."""

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from app.config.settings import Settings
from app.infrastructure.storage.object_key import names_internal_storage

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}


class InvalidExternalSource(ValueError):
    """The URL or returned content cannot be safely used for this operation."""


def _host_and_port(url: str) -> tuple[str, int]:
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise InvalidExternalSource("Invalid external URL")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.hostname not in {"localhost", "metadata.google.internal"}
            and parsed.username is None
        ):
            host = parsed.hostname.encode("idna").decode("ascii").rstrip(".")
            return host, parsed.port or (443 if parsed.scheme == "https" else 80)
    except (UnicodeError, ValueError):
        pass
    raise InvalidExternalSource("Invalid external URL")


def _public_address(addresses: Iterable[str]) -> str:
    resolved = [ipaddress.ip_address(value) for value in addresses]
    if not resolved or any(not addr.is_global or addr.is_multicast for addr in resolved):
        raise InvalidExternalSource("External URL must resolve to public addresses")
    return str(resolved[0])


def is_safe_url(url: str) -> bool:
    """Synchronous URL guard retained for existing callers doing validation."""
    try:
        host, port = _host_and_port(url)
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        _public_address(str(result[4][0]) for result in results)
    except (OSError, ValueError):
        return False
    return True


async def _resolve_public_address(host: str, port: int) -> str:
    try:
        results = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_NONAME:
            raise InvalidExternalSource("External source host could not be found") from exc
        raise httpx.ConnectError("External host resolution failed") from exc
    except OSError as exc:
        raise httpx.ConnectError("External host resolution failed") from exc
    return _public_address(str(result[4][0]) for result in results)


async def fetch_external(
    url: str,
    settings: Settings,
    *,
    timeout: float,
    max_bytes: int,
    accepts: Callable[[str], bool],
) -> tuple[httpx.Response, str]:
    """Fetch once, revalidate each redirect and return the original final URL.

    Connect to the checked address so a change in DNS cannot turn the HTTP
    request into a second, unchecked lookup. Keep the logical Host and TLS
    server name, and disable idle connection reuse across names on one IP.
    """
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
            headers=BROWSER_HEADERS,
        ) as client:
            for _ in range(6):
                if names_internal_storage(url, settings):
                    raise InvalidExternalSource("Internal storage URL cannot be fetched as an external source")
                host, port = _host_and_port(url)
                address = await _resolve_public_address(host, port)
                try:
                    original = httpx.URL(url)
                except httpx.InvalidURL as exc:
                    raise InvalidExternalSource("Invalid external URL") from exc
                host_header = original.netloc.decode("ascii")
                async with client.stream(
                    "GET",
                    original.copy_with(host=address),
                    headers={"Host": host_header},
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise InvalidExternalSource("External source returned a missing redirect URL")
                        try:
                            url = urljoin(url, location)
                        except ValueError as exc:
                            raise InvalidExternalSource("Invalid redirect URL") from exc
                        continue
                    response.raise_for_status()
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if not accepts(media_type):
                        raise InvalidExternalSource("Unsupported external source content type")
                    return await _read_bounded(response, max_bytes), url
    raise InvalidExternalSource("External source redirected too many times")


async def _read_bounded(response: httpx.Response, max_bytes: int) -> httpx.Response:
    content_length = response.headers.get("content-length")
    if content_length and content_length.isdecimal() and int(content_length) > max_bytes:
        raise InvalidExternalSource("External source is too large")
    # Read raw bytes to avoid decompressing unbounded content in one chunk.
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise InvalidExternalSource("Unsupported external source encoding")
    content = bytearray()
    chunks = response.aiter_bytes() if response.is_stream_consumed else response.aiter_raw()
    async for chunk in chunks:
        if len(content) + len(chunk) > max_bytes:
            raise InvalidExternalSource("External source is too large")
        content.extend(chunk)
    return httpx.Response(response.status_code, headers=response.headers, content=bytes(content))
