"""Content-hash parse cache for idempotent re-ingest.

Cache key includes parser/embed/config versions so dependency upgrades
automatically miss stale entries.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Any

from schemas.ingest import ParseResponse


def content_hash(
    *,
    file_type: str,
    file_bytes: bytes | None,
    markdown: str | None,
    chunk_size: int,
    chunk_overlap: int,
    use_vlm: bool,
    parser_version: str = "1.1",
    embed_model: str = "",
    config_version: str = "1",
) -> str:
    """Stable hash for parse inputs + dependency versions (not source_url body)."""
    h = hashlib.sha256()
    h.update(file_type.encode("utf-8"))
    h.update(b"|")
    h.update(str(chunk_size).encode())
    h.update(b"|")
    h.update(str(chunk_overlap).encode())
    h.update(b"|")
    h.update(b"1" if use_vlm else b"0")
    h.update(b"|")
    h.update((parser_version or "").encode("utf-8"))
    h.update(b"|")
    h.update((embed_model or "").encode("utf-8"))
    h.update(b"|")
    h.update((config_version or "").encode("utf-8"))
    h.update(b"|")
    if file_bytes is not None:
        h.update(file_bytes)
    elif markdown is not None:
        h.update(markdown.encode("utf-8"))
    return h.hexdigest()


class ParseResultCache:
    """LRU cache: content hash → parse result dict. Process-local."""

    def __init__(self, max_items: int = 256) -> None:
        self._max = max(1, max_items)
        self._lock = threading.Lock()
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def snapshot_size(self) -> int:
        with self._lock:
            return len(self._data)


_cache: ParseResultCache | None = None
_lock = threading.Lock()


def get_parse_cache(max_items: int = 256) -> ParseResultCache:
    global _cache
    with _lock:
        if _cache is None:
            _cache = ParseResultCache(max_items=max_items)
        return _cache


def to_cached(result: ParseResponse) -> dict[str, Any]:
    return result.model_dump()
