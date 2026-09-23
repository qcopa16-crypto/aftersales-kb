"""Content-hash parse cache for idempotent re-ingest.

Cache key includes parser/embed/config versions so dependency upgrades
automatically miss stale entries. Disk tier survives process restarts and
covers spooled large uploads via streaming file hash.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
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


def file_content_hash(
    path: Path | str,
    *,
    file_type: str,
    chunk_size: int,
    chunk_overlap: int,
    use_vlm: bool,
    parser_version: str = "1.1",
    embed_model: str = "",
    config_version: str = "1",
) -> str:
    """Streaming sha256 of on-disk upload + parse config (for spooled large files)."""
    p = Path(path)
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
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class DiskParseCache:
    """JSON file cache: content hash → parse payload. Process-safe enough for single writer."""

    def __init__(self, root: Path, max_entries: int = 128) -> None:
        self._root = Path(root)
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c for c in key if c.isalnum())[:64] or "x"
        return self._root / f"{safe}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            with self._lock:
                p.touch()
            return data
        except Exception:
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        p = self._path(key)
        try:
            with self._lock:
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                tmp.replace(p)
                self._evict_locked()
        except Exception:
            pass

    def _evict_locked(self) -> None:
        try:
            files = [f for f in self._root.glob("*.json")]
            if len(files) <= self._max:
                return
            files.sort(key=lambda f: f.stat().st_mtime)
            for f in files[: len(files) - self._max]:
                f.unlink(missing_ok=True)
        except Exception:
            pass


_disk: DiskParseCache | None = None
_disk_lock = threading.Lock()


def get_disk_parse_cache(path: str | Path, max_entries: int = 128) -> DiskParseCache:
    global _disk
    p = Path(path)
    with _disk_lock:
        if _disk is None or _disk._root != p:
            _disk = DiskParseCache(p, max_entries=max_entries)
        return _disk
