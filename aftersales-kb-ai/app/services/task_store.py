"""In-memory ingest task registry (Init-Poll). Process-local; not durable."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestTask:
    task_id: str
    unit_id: str
    file_type: str
    status: str = "queued"  # queued|running|done|failed
    stage: str = "queued"  # queued|fetch|parse|vlm|chunk|done|failed
    progress: int = 0  # 0-100
    detail: str = ""
    error_code: str | None = None
    error_message: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False

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
        }


class TaskStore:
    def __init__(self, max_tasks: int = 500) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, IngestTask] = {}
        self._max = max_tasks

    def create(self, unit_id: str, file_type: str) -> IngestTask:
        tid = uuid.uuid4().hex
        task = IngestTask(task_id=tid, unit_id=unit_id, file_type=file_type)
        with self._lock:
            if len(self._tasks) >= self._max:
                # drop oldest finished
                finished = [t for t in self._tasks.values() if t.status in {"done", "failed"}]
                finished.sort(key=lambda t: t.updated_at)
                for old in finished[: max(1, len(finished) // 2)]:
                    self._tasks.pop(old.task_id, None)
            self._tasks[tid] = task
        return task

    def get(self, task_id: str) -> IngestTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for k, v in kwargs.items():
                setattr(task, k, v)
            task.touch()

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
            items = sorted(self._tasks.values(), key=lambda t: t.updated_at, reverse=True)
            return [t.to_public() for t in items[:limit]]


_store: TaskStore | None = None
_store_lock = threading.Lock()


def get_task_store() -> TaskStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = TaskStore()
        return _store
