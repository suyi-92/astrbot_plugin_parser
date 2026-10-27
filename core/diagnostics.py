"""Opt-in timing logs. Never pass URLs, credentials or exception messages here."""

import asyncio
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from astrbot.api import logger

_TRACE = ContextVar("parser_diagnostic_trace", default="-")


def note(enabled, message):
    if enabled:
        logger.debug(f"[解析诊断 {_TRACE.get()}] {message}")


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
