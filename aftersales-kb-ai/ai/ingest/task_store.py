"""In-memory ingest task registry with lease/stale. Process-local; not durable.

Extension point for multi-replica: implement TaskStoreBackend against Redis/SQL
and swap get_task_store(); keep the same method surface (create/get/update/
heartbeat/mark_stale/request_cancel).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class IngestTask:
    task_id: str
    unit_id: str
    file_type: str
    status: str = "queued"  # queued|running|done|failed|stale
    stage: str = "queued"  # queued|fetch|parse|vlm|chunk|done|failed|stale
    progress: int = 0  # 0-100
    detail: str = ""
    error_code: str | None = None
    error_message: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    # lease / multi-attempt
    lease_until: float = 0.0
    heartbeat_at: float = field(default_factory=time.time)
    attempt: int = 0
    worker_id: str = ""

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "unit_id": self.unit_id,
            "file_type": self.file_type,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "detail": self.detail,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lease_until": self.lease_until,
            "heartbeat_at": self.heartbeat_at,
            "attempt": self.attempt,
            "worker_id": self.worker_id,
        }


class TaskStoreBackend(Protocol):
    """Future Redis/DB backends must implement this surface."""

    def create(self, unit_id: str, file_type: str) -> IngestTask: ...
    def get(self, task_id: str) -> IngestTask | None: ...
    def update(self, task_id: str, **kwargs: Any) -> None: ...
    def heartbeat(self, task_id: str, worker_id: str | None = None) -> bool: ...
    def mark_stale(self, task_id: str) -> bool: ...
    def request_cancel(self, task_id: str) -> bool: ...


class TaskStore:
    def __init__(self, max_tasks: int = 500, lease_sec: float = 120.0, reap_on_read: bool = True) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, IngestTask] = {}
        self._max = max_tasks
        self._lease_sec = max(0.01, lease_sec)
        self._reap_on_read = reap_on_read

    def create(self, unit_id: str, file_type: str) -> IngestTask:
        tid = uuid.uuid4().hex
        now = time.time()
        task = IngestTask(
            task_id=tid,
            unit_id=unit_id,
            file_type=file_type,
            lease_until=now + self._lease_sec,
            heartbeat_at=now,
            worker_id=f"proc-{uuid.uuid4().hex[:8]}",
        )
        with self._lock:
            if len(self._tasks) >= self._max:
                finished = [t for t in self._tasks.values() if t.status in {"done", "failed", "stale"}]
                finished.sort(key=lambda t: t.updated_at)
                for old in finished[: max(1, len(finished) // 2)]:
                    self._tasks.pop(old.task_id, None)
            self._tasks[tid] = task
        return task

    def _reap_locked(self) -> None:
        now = time.time()
        for task in self._tasks.values():
            if task.status != "running":
                continue
            if task.lease_until and now > task.lease_until:
                task.status = "stale"
                task.stage = "stale"
                task.error_code = "TASK_STALE"
                task.error_message = f"lease expired after {self._lease_sec}s"
                task.touch()
                logger.warning("task marked stale task_id=%s unit_id=%s", task.task_id, task.unit_id)

    def get(self, task_id: str) -> IngestTask | None:
        with self._lock:
            if self._reap_on_read:
                self._reap_locked()
            return self._tasks.get(task_id)

    def update(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for k, v in kwargs.items():
                setattr(task, k, v)
            # Terminal success must not keep a stale error residue.
            if task.status == "done" and "error_code" not in kwargs:
                task.error_code = None
                task.error_message = None
            task.touch()
            # Live worker progress renews lease; if reaper marked stale, re-assert running.
            if task.status == "stale" and kwargs.get("status") is None and "progress" in kwargs:
                task.status = "running"
                task.stage = kwargs.get("stage") or task.stage
                task.error_code = None
                task.error_message = None
            if task.status == "running":
                task.heartbeat_at = task.updated_at
                task.lease_until = task.updated_at + self._lease_sec

    def heartbeat(self, task_id: str, worker_id: str | None = None) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in {"queued", "running"}:
                return False
            if worker_id and task.worker_id and task.worker_id != worker_id:
                return False
            now = time.time()
            task.heartbeat_at = now
            task.lease_until = now + self._lease_sec
            task.touch()
            return True

    def mark_stale(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status in {"done", "failed"}:
                return False
            task.status = "stale"
            task.stage = "stale"
            task.error_code = "TASK_STALE"
            task.error_message = "marked stale"
            task.touch()
            return True

    def requeue_stale(self, task_id: str) -> IngestTask | None:
        """Allow orchestrator to re-run a stale task (attempt+1)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != "stale":
                return None
            now = time.time()
            task.status = "queued"
            task.stage = "queued"
            task.progress = 0
            task.error_code = None
            task.error_message = None
            task.cancel_requested = False
            task.attempt += 1
            task.lease_until = now + self._lease_sec
            task.heartbeat_at = now
            task.touch()
            return task

    def request_cancel(self, task_id: str) -> bool:
        """Mark cancel; running worker checks cancel_requested between stages."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status in {"done", "failed"}:
                return False
            task.cancel_requested = True
            if task.status == "queued":
                task.status = "failed"
                task.stage = "failed"
                task.error_code = "TASK_CANCELLED"
                task.error_message = "cancelled before start"
            task.touch()
            return True

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            return bool(task and task.cancel_requested)

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if self._reap_on_read:
                self._reap_locked()
            items = sorted(self._tasks.values(), key=lambda t: t.updated_at, reverse=True)
            return [t.to_public() for t in items[:limit]]

    def active_count(self) -> int:
        with self._lock:
            if self._reap_on_read:
                self._reap_locked()
            return sum(1 for t in self._tasks.values() if t.status in {"queued", "running"})


_store: TaskStore | None = None
_store_lock = threading.Lock()


def get_task_store(lease_sec: float | None = None, reap_on_read: bool | None = None) -> TaskStore:
    global _store
    with _store_lock:
        if _store is None:
            from core.config import get_settings

            cfg = get_settings()
            _store = TaskStore(
                lease_sec=lease_sec if lease_sec is not None else cfg.task_lease_sec,
                reap_on_read=reap_on_read if reap_on_read is not None else cfg.task_reap_on_read,
            )
        return _store


def reset_task_store_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
