"""Disk-backed MinerU batch_id store for timeout recovery (avoid re-submit)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class MinerUBatchStore:
    def __init__(self, path: Path, max_entries: int = 500) -> None:
        self._path = path
        self._max = max(1, max_entries)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        if len(data) > self._max:
            items = sorted(data.items(), key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
            data = dict(items[: self._max])
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def get_batch_id(self, file_hash: str) -> str | None:
        with self._lock:
            entry = self._load().get(file_hash)
            if not entry:
                return None
            return entry.get("batch_id")

    def put(self, file_hash: str, batch_id: str, *, file_name: str = "") -> None:
        with self._lock:
            data = self._load()
            data[file_hash] = {
                "batch_id": batch_id,
                "file_name": file_name,
                "updated_at": time.time(),
            }
            self._save(data)

    def clear(self, file_hash: str) -> None:
        with self._lock:
            data = self._load()
            if file_hash in data:
                del data[file_hash]
                self._save(data)


_store: MinerUBatchStore | None = None
_store_lock = threading.Lock()


def get_mineru_batch_store(path: str | Path) -> MinerUBatchStore:
    global _store
    p = Path(path)
    with _store_lock:
        if _store is None or _store._path != p:
            _store = MinerUBatchStore(p)
        return _store
