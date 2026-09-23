"""Disk cache for MinerU extract results (markdown + images) keyed by file hash + options."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def extract_cache_key(
    file_hash: str,
    *,
    model_version: str = "",
    language: str = "",
    enable_table: bool = True,
    enable_formula: bool = True,
    is_ocr: bool = False,
) -> str:
    h = hashlib.sha256()
    h.update((file_hash or "").encode("utf-8"))
    h.update(b"|")
    h.update((model_version or "").encode("utf-8"))
    h.update(b"|")
    h.update((language or "").encode("utf-8"))
    h.update(b"|")
    h.update(b"1" if enable_table else b"0")
    h.update(b"|")
    h.update(b"1" if enable_formula else b"0")
    h.update(b"|")
    h.update(b"1" if is_ocr else b"0")
    return h.hexdigest()[:40]


class MinerUExtractCache:
    def __init__(self, root: Path, max_entries: int = 200) -> None:
        self._root = Path(root)
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def _entry_dir(self, key: str) -> Path:
        return self._root / key

    def get(self, key: str) -> tuple[str, int | None, list[dict]] | None:
        d = self._entry_dir(key)
        meta_path = d / "meta.json"
        md_path = d / "full.md"
        if not meta_path.is_file() or not md_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            images: list[dict] = []
            for im in meta.get("images") or []:
                rel = str(im.get("relative_path") or "")
                blob = b""
                img_file = d / "images" / Path(rel).name
                if img_file.is_file():
                    blob = img_file.read_bytes()
                images.append(
                    {
                        "image_id": im.get("image_id"),
                        "relative_path": rel,
                        "status": im.get("status") or "skipped",
                        "caption": im.get("caption"),
                        "bytes": blob,
                    }
                )
            with self._lock:
                try:
                    meta_path.touch()
                except OSError:
                    pass
            return markdown, meta.get("page_count"), images
        except Exception:
            logger.warning("mineru extract cache read failed key=%s", key, exc_info=True)
            return None

    def put(self, key: str, markdown: str, page_count: int | None, images: list[dict]) -> None:
        d = self._entry_dir(key)
        try:
            with self._lock:
                d.mkdir(parents=True, exist_ok=True)
                (d / "full.md").write_text(markdown or "", encoding="utf-8")
                img_meta = []
                img_dir = d / "images"
                if images:
                    img_dir.mkdir(parents=True, exist_ok=True)
                for im in images:
                    rel = str(im.get("relative_path") or "")
                    blob = im.get("bytes") or b""
                    name = Path(rel).name or f"{im.get('image_id')}.bin"
                    if blob:
                        (img_dir / name).write_bytes(blob)
                    img_meta.append(
                        {
                            "image_id": im.get("image_id"),
                            "relative_path": rel,
                            "status": im.get("status") or "skipped",
                            "caption": im.get("caption"),
                            "file_name": name if blob else None,
                        }
                    )
                meta = {
                    "page_count": page_count,
                    "images": img_meta,
                    "updated_at": time.time(),
                }
                (d / "meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self._evict_locked()
        except Exception:
            logger.warning("mineru extract cache write failed key=%s", key, exc_info=True)

    def _evict_locked(self) -> None:
        try:
            entries = [p for p in self._root.iterdir() if p.is_dir()]
            if len(entries) <= self._max:
                return
            entries.sort(key=lambda p: p.stat().st_mtime)
            for p in entries[: len(entries) - self._max]:
                for child in sorted(p.rglob("*"), reverse=True):
                    try:
                        child.unlink()
                    except OSError:
                        pass
                try:
                    p.rmdir()
                except OSError:
                    pass
        except Exception:
            pass


_cache: MinerUExtractCache | None = None
_lock = threading.Lock()


def get_mineru_extract_cache(path: str | Path, max_entries: int = 200) -> MinerUExtractCache:
    global _cache
    p = Path(path)
    with _lock:
        if _cache is None or _cache._root != p:
            _cache = MinerUExtractCache(p, max_entries=max_entries)
        return _cache
