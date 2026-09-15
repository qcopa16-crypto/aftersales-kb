"""Request-scoped trace_id via contextvars. No external tracer."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id or "-")


def get_trace_id() -> str:
    return _trace_id.get()


def reset_trace_id(token) -> None:  # type: ignore[no-untyped-def]
    _trace_id.reset(token)


class TraceIdFilter(logging.Filter):
    """Inject trace_id into every log record for format %(trace_id)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = get_trace_id()
        return True


def install_trace_logging() -> None:
    """Attach TraceIdFilter to root handlers; safe to call multiple times."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(TraceIdFilter())
        fmt = handler.formatter
        if fmt is not None and "trace_id" not in (fmt._fmt or ""):
            handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s %(levelname)s [%(trace_id)s] %(name)s %(message)s"
                )
            )
