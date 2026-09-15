"""Stable fingerprints for parse/embed batch consistency."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence


def content_fingerprint(markdown: str | None) -> str:
    return hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()


def chunks_fingerprint(chunks: Sequence[Mapping[str, Any]]) -> str:
    """Hash ordered chunks: chunk_index + content + heading_path."""
    h = hashlib.sha256()
    for c in chunks:
        idx = c.get("chunk_index")
        content = c.get("content") or ""
        heading_path = c.get("heading_path") or []
        h.update(str(int(idx) if idx is not None else 0).encode("utf-8"))
        h.update(b"\x1f")
        h.update(content.encode("utf-8"))
        h.update(b"\x1f")
        h.update("\x1e".join(str(x) for x in heading_path).encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()
