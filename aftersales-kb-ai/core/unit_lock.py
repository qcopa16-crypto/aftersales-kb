"""Per-unit_id locks to serialize concurrent embed/delete for the same unit."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class UnitLockRegistry:
    def __init__(self) -> None:
        self._meta = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._refs: dict[str, int] = {}

    @contextmanager
    def hold(self, unit_id: str) -> Iterator[None]:
        with self._meta:
            lock = self._locks.get(unit_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[unit_id] = lock
            self._refs[unit_id] = self._refs.get(unit_id, 0) + 1
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._meta:
                n = self._refs.get(unit_id, 0) - 1
                if n <= 0:
                    self._refs.pop(unit_id, None)
                    self._locks.pop(unit_id, None)
                else:
                    self._refs[unit_id] = n


_registry: UnitLockRegistry | None = None
_registry_lock = threading.Lock()


def get_unit_locks() -> UnitLockRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = UnitLockRegistry()
        return _registry
