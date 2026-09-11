"""Process-local partial embed checkpoints for resume."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class EmbedPartial:
    unit_id: str
    dim: int
    vectors: list[list[float]]  # aligned to chunk_index order
    contents: list[str]


class EmbedPartialStore:
    def __init__(self, max_units: int = 32) -> None:
        self._max = max(1, max_units)
        self._lock = threading.Lock()
        self._data: OrderedDict[str, EmbedPartial] = OrderedDict()

    def put(self, partial: EmbedPartial) -> None:
        with self._lock:
            self._data[partial.unit_id] = partial
            self._data.move_to_end(partial.unit_id)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def get(self, unit_id: str) -> EmbedPartial | None:
        with self._lock:
            p = self._data.get(unit_id)
            if p:
                self._data.move_to_end(unit_id)
            return p

    def clear(self, unit_id: str) -> None:
        with self._lock:
            self._data.pop(unit_id, None)

    def snapshot_size(self) -> int:
        with self._lock:
            return len(self._data)


_store: EmbedPartialStore | None = None
_lock = threading.Lock()


def get_embed_partial_store(max_units: int = 32) -> EmbedPartialStore:
    global _store
    with _lock:
        if _store is None:
            _store = EmbedPartialStore(max_units=max_units)
        return _store
