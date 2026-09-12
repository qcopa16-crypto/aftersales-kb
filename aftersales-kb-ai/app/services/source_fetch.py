"""Fetch remote source (MinIO presigned URL) for parse. SSRF-guarded with DNS pin."""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.utils.circuit import get_circuits, get_metrics
from app.utils.retry import RetryPolicy, retry_call


class SourceFetchError(RuntimeError):
    def __init__(self, message: str, error_code: str = "SOURCE_FETCH_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


def _is_private_ip(ip: str) -> bool:
    """True only when `ip` is an IP literal that is private/reserved/etc.

    Hostnames are NOT handled here — callers must resolve first. Returning
    True on parse failure would block every domain name before DNS.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve_public_ip(host: str, port: int) -> str:
    """Resolve host and return first non-private IPv4/IPv6. Raise on private/gaierror."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SourceFetchError(f"cannot resolve host: {host}", "SOURCE_BAD_URL") from exc
    if not infos:
        raise SourceFetchError(f"cannot resolve host: {host}", "SOURCE_BAD_URL")
    for info in infos:
        ip = info[4][0]
        if not _is_private_ip(ip):
            return ip
    # All resolved addresses private/loopback.
    sample = infos[0][4][0]
    raise SourceFetchError(
        f"source_url resolves to private IP: {sample}", "SOURCE_SSRF_BLOCKED"
    )


def _validate_url(url: str, *, allow_private_hosts: bool = False) -> str:
    """Validate scheme/host and return the IP that the connection must use (DNS pin).

    Returns the literal host when allow_private_hosts is set (test-only).
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SourceFetchError(
            f"unsupported source_url scheme: {parsed.scheme or '(empty)'}", "SOURCE_BAD_URL"
        )
    if not parsed.netloc:
        raise SourceFetchError("source_url missing host", "SOURCE_BAD_URL")
    host = parsed.hostname
    if not host:
        raise SourceFetchError("source_url missing host", "SOURCE_BAD_URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # SSRF: block literal private/loopback; resolve hostname and re-check.
    # allow_private_hosts is for local fault-injection tests only.
    if allow_private_hosts:
        return host
    if _is_ip_literal(host) and _is_private_ip(host):
        raise SourceFetchError(
            f"source_url host blocked (private/loopback): {host}", "SOURCE_SSRF_BLOCKED"
        )
    return _resolve_public_ip(host, port)


def _host_header(host: str, port: int | None, scheme: str) -> str:
    """RFC Host header: include port when non-default for the scheme."""
    default = 443 if scheme == "https" else 80
    if port and port != default:
        return f"{host}:{port}"
    return host


def _pinned_url(original: str, ip: str) -> tuple[str, str]:
    """Rewrite URL so the TCP connection targets `ip`; Host/SNI stay on the original host."""
    parsed = urlparse(original)
    host = parsed.hostname or ""
    port = parsed.port
    scheme = parsed.scheme or "http"
    host_hdr = _host_header(host, port, scheme)
    # Bracket IPv6 literals.
    netloc = f"[{ip}]" if ":" in ip else ip
    if port:
        netloc = f"{netloc}:{port}"
    # Preserve userinfo if any (rare for presigned URLs).
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        netloc = f"{auth}@{netloc}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{netloc}{path}{query}", host_hdr


def _fetch_source_once(
    url: str,
    dest_dir: Path,
    *,
    filename: str | None,
    cfg: Settings,
) -> Path:
    """Download URL into dest_dir; return local path. Enforces size limit.

    Redirects are followed manually so every hop is SSRF-validated and DNS-pinned.
    """
    timeout = httpx.Timeout(
        cfg.source_read_timeout_sec,
        connect=cfg.source_connect_timeout_sec,
    )
    current = url
    max_hops = max(0, cfg.source_max_redirects)
    allow_private = bool(cfg.source_allow_private_hosts)
    try:
        for hop in range(max_hops + 1):
            pin_ip = _validate_url(current, allow_private_hosts=allow_private)
            connect_url, host_hdr = _pinned_url(current, pin_ip)
            headers: dict[str, str] = {"Host": host_hdr}
            extensions: dict = {}
            if urlparse(current).scheme == "https":
                # Keep TLS SNI/cert validation on the original hostname while
                # dialing the already-validated IP (closes DNS rebinding TOCTOU).
                extensions["sni_hostname"] = urlparse(current).hostname or host_hdr
            stream_kwargs: dict = {
                "timeout": timeout,
                "follow_redirects": False,
                "headers": headers,
            }
            if extensions:
                stream_kwargs["extensions"] = extensions
            with httpx.stream("GET", connect_url, **stream_kwargs) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not location:
                        raise SourceFetchError(
                            f"source redirect without Location (HTTP {resp.status_code})",
                            "SOURCE_FETCH_FAILED",
                        )
                    if hop >= max_hops:
                        raise SourceFetchError(
                            f"source exceeded max redirects ({max_hops})",
                            "SOURCE_FETCH_FAILED",
                        )
                    current = str(httpx.URL(current).join(location))
                    continue
                if resp.status_code >= 400:
                    # 5xx/429 are transient; other 4xx are permanent client faults.
                    if resp.status_code >= 500 or resp.status_code == 429:
                        raise SourceFetchError(
                            f"source fetch HTTP {resp.status_code}",
                            "SOURCE_FETCH_FAILED",
                        )
                    raise SourceFetchError(
                        f"source fetch HTTP {resp.status_code}",
                        "SOURCE_CLIENT_ERROR",
                    )
                name = filename or Path(urlparse(current).path).name or "source.bin"
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
        raise SourceFetchError("source redirect loop", "SOURCE_FETCH_FAILED")
    except SourceFetchError:
        raise
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"source fetch failed: {exc}", "SOURCE_FETCH_FAILED") from exc


def fetch_source_to_temp(
    url: str,
    dest_dir: Path,
    *,
    filename: str | None,
    cfg: Settings,
    policy: RetryPolicy | None = None,
    sleep=None,
) -> Path:
    """Download with classified retries and process-local circuit breaker."""
    circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
    metrics = get_metrics()
    if circuits.is_open("source_fetch"):
        metrics.inc("source.circuit_open")
        raise SourceFetchError("source_fetch circuit open (cooldown)", "SOURCE_CIRCUIT_OPEN")

    kwargs = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    try:
        path = retry_call(
            _fetch_source_once,
            url,
            dest_dir,
            filename=filename,
            cfg=cfg,
            policy=policy
            or RetryPolicy(
                max_attempts=3,
                base_delay_sec=0.2,
                max_delay_sec=5.0,
                multiplier=2.0,
            ),
            **kwargs,
        )
    except SourceFetchError as exc:
        # Client/SSRF faults are not dependency health signals.
        if exc.error_code not in {
            "SOURCE_BAD_URL",
            "SOURCE_SSRF_BLOCKED",
            "SOURCE_EMPTY",
            "SOURCE_CLIENT_ERROR",
            "SOURCE_TOO_LARGE",
            "SOURCE_CIRCUIT_OPEN",
        }:
            circuits.record_failure("source_fetch", str(exc))
        metrics.inc("source.fail")
        raise
    circuits.record_success("source_fetch")
    metrics.inc("source.ok")
    return path
