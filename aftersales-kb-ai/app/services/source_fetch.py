"""Fetch remote source (MinIO presigned URL) for parse. SSRF-guarded."""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Settings


class SourceFetchError(RuntimeError):
    def __init__(self, message: str, error_code: str = "SOURCE_FETCH_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SourceFetchError(f"unsupported source_url scheme: {parsed.scheme or '(empty)'}", "SOURCE_BAD_URL")
    if not parsed.netloc:
        raise SourceFetchError("source_url missing host", "SOURCE_BAD_URL")
    host = parsed.hostname
    if not host:
        raise SourceFetchError("source_url missing host", "SOURCE_BAD_URL")
    # SSRF: block literal private/loopback; resolve hostname and re-check
    if _is_private_ip(host):
        raise SourceFetchError(f"source_url host blocked (private/loopback): {host}", "SOURCE_SSRF_BLOCKED")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise SourceFetchError(f"cannot resolve host: {host}", "SOURCE_BAD_URL") from exc
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise SourceFetchError(f"source_url resolves to private IP: {ip}", "SOURCE_SSRF_BLOCKED")


def fetch_source_to_temp(
    url: str,
    dest_dir: Path,
    *,
    filename: str | None,
    cfg: Settings,
) -> Path:
    """Download URL into dest_dir; return local path. Enforces size limit."""
    _validate_url(url)
    timeout = httpx.Timeout(
        cfg.source_read_timeout_sec,
        connect=cfg.source_connect_timeout_sec,
    )
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            if resp.status_code >= 400:
                raise SourceFetchError(
                    f"source fetch HTTP {resp.status_code}",
                    "SOURCE_FETCH_FAILED",
                )
            name = filename or Path(urlparse(url).path).name or "source.bin"
            name = Path(name).name.replace("\\", "_")[:120] or "source.bin"
            dest = dest_dir / name
            max_bytes = cfg.max_source_bytes
            total = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        dest.unlink(missing_ok=True)
                        raise SourceFetchError(
                            f"source exceeds max size {max_bytes} bytes",
                            "SOURCE_TOO_LARGE",
                        )
                    f.write(chunk)
            if total == 0:
                dest.unlink(missing_ok=True)
                raise SourceFetchError("source is empty", "SOURCE_EMPTY")
            return dest
    except SourceFetchError:
        raise
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"source fetch failed: {exc}", "SOURCE_FETCH_FAILED") from exc
