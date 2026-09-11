"""VLM caption cache keyed by image sha256."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict


class CaptionCache:
    def __init__(self, max_items: int = 512) -> None:
        self._max = max(1, max_items)
        self._lock = threading.Lock()
        self._data: OrderedDict[str, str] = OrderedDict()

    @staticmethod
    def key(image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes).hexdigest()

    def get(self, image_bytes: bytes) -> str | None:
        k = self.key(image_bytes)
        with self._lock:
            if k not in self._data:
                return None
            self._data.move_to_end(k)
            return self._data[k]

    def put(self, image_bytes: bytes, caption: str) -> None:
        k = self.key(image_bytes)
        with self._lock:
            self._data[k] = caption
            self._data.move_to_end(k)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def size(self) -> int:
        with self._lock:
            return len(self._data)


_cache: CaptionCache | None = None
_lock = threading.Lock()


def get_caption_cache(max_items: int = 512) -> CaptionCache:
    global _cache
    with _lock:
        if _cache is None:
            _cache = CaptionCache(max_items=max_items)
        return _cache
