"""Shared error-body helpers: truncate outbound messages; strip control chars."""

from __future__ import annotations

import re
from typing import Any

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(value: str) -> str:
    """Remove control characters that can corrupt logs/prompts; keep newlines/tabs."""
    return _CONTROL_CHARS.sub("", value or "").strip()


def safe_message(exc: BaseException | str | None, max_chars: int = 500) -> str:
    """Human-facing message for HTTP detail / SSE error; never dumps full traceback."""
    if exc is None:
        return ""
    text = str(exc)
    if not text:
        return type(exc).__name__ if not isinstance(exc, str) else ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = _CONTROL_CHARS.sub("", text)
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def detail_body(
    error_code: str,
    message: str | BaseException | None,
    *,
    max_chars: int = 500,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error_code": error_code,
        "message": safe_message(message, max_chars=max_chars),
    }
    body.update(extra)
    return body
