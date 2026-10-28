"""Opt-in diagnostics. Log only safe labels, never URLs or exception details."""

import asyncio
import ipaddress
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from astrbot.api import logger

_TRACE = ContextVar("parser_diagnostic_trace", default="-")


def note(enabled, message):
    if enabled:
        logger.debug(f"[解析诊断 {_TRACE.get()}] {message}")


def _proxy_kind(value):
    if not value:
        return "empty"
    if isinstance(value, str):
        scheme = value.strip().partition(":")[0].lower()
        if scheme in ("vless", "http", "https"):
            return scheme
    return "other"


def proxy_route(cfg, actual_proxy):
    """Describe the exact argument passed to a Bilibili request, never its value.

    Merely having a VLESS link in the configuration is not evidence that a
    request uses it. Match the selected endpoint to the owned bridge instead.
    """
    options = getattr(getattr(cfg, "parser", None), "bilibili", None)
    enabled = bool(getattr(options, "use_proxy", False))
    configured = _proxy_kind(getattr(cfg, "proxy", None))
    manager = getattr(cfg, "_proxy_manager", None)
    endpoint = getattr(manager, "_endpoint", None)
    xray = "not_used"
    if not actual_proxy:
        route = "direct"
    elif endpoint and actual_proxy == endpoint:
        route = "vless+xray"
        process = getattr(manager, "process", None)
        xray = (
            "running"
            if process is not None and process.returncode is None
            else "stopped"
        )
    elif _proxy_kind(actual_proxy) in ("http", "https"):
        route = "http-proxy"
    else:
        route = "other-proxy"
    return (
        f"route={route} use_proxy={'on' if enabled else 'off'} "
        f"configured={configured} xray={xray}"
    )


def response_peer(response, actual_proxy):
    """Inspect the live socket without logging IPs, hosts, ports or credentials.

    aiohttp may release fully buffered responses before returning them. Report
    unknown in that case, never fabricate socket evidence from configuration.
    No DNS lookup or additional request is made for diagnostics.
    """
    peer_kind = "unavailable"
    proof = "unknown" if actual_proxy else "not_used"
    try:
        connection = getattr(response, "connection", None)
        transport = getattr(connection, "transport", None)
        peer = transport.get_extra_info("peername") if transport else None
        if peer:
            address = ipaddress.ip_address(peer[0])
            peer_kind = (
                "loopback"
                if address.is_loopback
                else "public"
                if address.is_global
                else "private"
            )
            if actual_proxy:
                parsed = urlsplit(actual_proxy)
                expected = ipaddress.ip_address(parsed.hostname or "")
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                proof = (
                    "confirmed"
                    if address == expected and peer[1] == port
                    else "mismatch"
                )
    except (AttributeError, TypeError, ValueError, OSError, IndexError):
        # A proxy hostname cannot be verified against its socket without another
        # DNS query. This affects diagnostics only, not the request itself.
        pass
    return f"socket_peer={peer_kind} proxy_socket={proof}"


@contextmanager
def trace_scope(enabled):
    # Child download/send tasks inherit this ID; separate messages get separate IDs.
    token = _TRACE.set(uuid.uuid4().hex[:10]) if enabled else None
    try:
        yield
    finally:
        if token is not None:
            _TRACE.reset(token)


@contextmanager
def stage(enabled, name):
    if not enabled:
        yield
        return
    started = time.monotonic()
    note(True, f"{name} 开始")
    outcome = "ok"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException as exc:
        # Exception strings can contain cookies or signed media URLs.
        outcome = f"failed error={type(exc).__name__}"
        raise
    finally:
        note(
            True,
            f"{name} 结束 elapsed_s={time.monotonic() - started:.2f} result={outcome}",
        )


class TransferProgress:
    """Rate-limit progress logs without background tasks or modifying transport."""

    interval = 10.0

    def __init__(self, enabled, stream, host, offset, total):
        self.enabled, self.stream, self.host = enabled, stream, host
        self.offset, self.total = offset, total
        self.at = time.monotonic() if enabled else 0

    def update(self, offset, *, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        elapsed = now - self.at
        if not force and elapsed < self.interval:
            return
        rate = (offset - self.offset) / max(elapsed, 0.001) / 1024
        total = f"{self.total / 1024**2:.2f}" if self.total is not None else "unknown"
        note(
            True,
            f"下载进度 stream={self.stream} cdn={self.host} "
            f"done_mib={offset / 1024**2:.2f} total_mib={total} rate_kib_s={rate:.1f}",
        )
        self.at, self.offset = now, offset
