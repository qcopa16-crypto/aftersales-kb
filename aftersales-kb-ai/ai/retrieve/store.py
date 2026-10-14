"""Vector store backends: Milvus and in-memory. Supports versioned atomic publish."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from core.config import Settings
from core.circuit import get_metrics

logger = logging.getLogger(__name__)

# Legacy content cap (Milvus VARCHAR practical guard).
MAX_CONTENT_CHARS = 60000


class ActiveMapLockError(RuntimeError):
    """Raised when the cross-process active-map lock cannot be acquired (R-06).

    Fail-closed by design: a shared active-version file written without the
    lock can silently lose a concurrent publish (last writer wins on the whole
    file). Callers must surface this and retry rather than proceed unlocked.
    """


class ActiveMapPersistenceError(RuntimeError):
    """Raised when the active-version map could not be persisted (R-02).

    Covers the actual file I/O path — temp-file write, ``os.replace`` and the
    post-write ``stat`` — not just the lock. A swallowed persistence failure
    yields "vectors published but the mapping never landed": after a restart the
    new version is invisible (or a deleted mapping still points at removed
    vectors). Callers (``publish_unit`` / ``delete_by_unit``) must fail and
    retry rather than report success; only the rollback-cleanup path may swallow
    it (the original error is the one worth surfacing).
    """


@contextmanager
def _cross_process_lock(
    path: Path, timeout: float = 5.0, *, fail_closed: bool = True
) -> Iterator[None]:
    """Cross-process mutex via atomic lock-file creation (R-06).

    ``fcntl``/``msvcrt`` differ across platforms, so this uses the portable
    ``O_CREAT | O_EXCL`` create-or-fail primitive. A lock older than
    ``timeout * 4`` seconds is treated as stale (holder crashed) and reclaimed.

    When ``fail_closed`` is true (the default, and forced for multi-replica
    safe mode) a lock timeout or a lock-directory/permission error raises
    ``ActiveMapLockError`` instead of proceeding unlocked — the caller retries.
    Setting it false restores the legacy fail-open behaviour and is only meant
    for single-writer deployments where the lock is a courtesy.

    KNOWN LIMITATION (documented, not a bug): even fail-closed, this is a
    mutual-exclusion hint, **not** a compare-and-swap. Under extreme contention
    (or when a holder is stalled past the stale threshold and its lock is
    reclaimed) two writers can interleave and one update can be lost. It also
    cannot express a per-unit compare-and-swap. For strict multi-replica
    consistency the active-version map should move to a store with atomic CAS
    (MySQL row / Redis ``SET NX`` + Lua compare), not a shared file.
    """
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                st = os.stat(str(lock_path))
                if time.time() - st.st_mtime > timeout * 4:
                    os.unlink(str(lock_path))
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                if fail_closed:
                    raise ActiveMapLockError(
                        f"active map lock not acquired within {timeout}s path={lock_path}"
                    )
                logger.warning("active map lock timeout; proceeding unlocked path=%s", lock_path)
                break
            time.sleep(0.01)
        except OSError as exc:
            if fail_closed:
                raise ActiveMapLockError(
                    f"active map lock unavailable path={lock_path}: {exc}"
                ) from exc
            # Lock dir missing / permission: proceed unlocked rather than fail.
            break
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass


class PublishValidationError(RuntimeError):
    def __init__(self, message: str, error_code: str = "PUBLISH_VALIDATE_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class SearchFilter:
    """Executable retrieve filter. Java resolves identity; Python only applies conditions.

    unit_ids/deny_unit_ids: None = unconstrained; [] = matches nothing (fail-closed).
    """

    unit_ids: list[str] | None = None
    deny_unit_ids: list[str] | None = None
    department: str | None = None
    classification: str | None = None

    def __post_init__(self) -> None:
        # Precompute membership sets once; row_passes_filter is a hot per-row path.
        self._unit_set: frozenset[str] | None = (
            frozenset(self.unit_ids) if self.unit_ids is not None else None
        )
        self._deny_set: frozenset[str] | None = (
            frozenset(self.deny_unit_ids) if self.deny_unit_ids is not None else None
        )

    def has_constraints(self) -> bool:
        return (
            self.unit_ids is not None
            or self.deny_unit_ids is not None
            or bool((self.department or "").strip())
            or bool((self.classification or "").strip())
        )

    def is_empty(self) -> bool:
        """True only when no ACL constraint applies. Empty allowlist is NOT empty."""
        return not self.has_constraints()


def row_passes_filter(row: dict, flt: SearchFilter | None) -> bool:
    if flt is None:
        return True
    unit = str(row.get("unit_id") or "")
    unit_set = getattr(flt, "_unit_set", None)
    if unit_set is not None and unit not in unit_set:
        return False
    deny_set = getattr(flt, "_deny_set", None)
    if deny_set and unit in deny_set:
        return False
    dept = (flt.department or "").strip()
    if dept:
        row_dept = str(row.get("acl_department") or "")
        if row_dept != dept:
            return False
    classification = (flt.classification or "").strip()
    if classification:
        row_cls = str(row.get("acl_classification") or "")
        if row_cls != classification:
            return False
    return True


@dataclass
class VectorItem:
    chunk_index: int
    content: str
    embedding: list[float]
    embed_model: str = ""
    version_id: str = ""
    ingest_id: str = ""
    acl_department: str = ""
    acl_classification: str = ""
    heading_path: list[str] | None = None


def _stable_id(unit_id: str, chunk_index: int, version_id: str = "") -> str:
    if version_id:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"{unit_id}::{chunk_index}::{version_id}").hex
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{unit_id}::{chunk_index}").hex


def _new_version_id() -> str:
    return uuid.uuid4().hex


class VectorStore(ABC):
    @abstractmethod
    def ensure_collection(self) -> None: ...

    @abstractmethod
    def upsert(self, unit_id: str, items: list[VectorItem]) -> int: ...

    def publish_unit(self, unit_id: str, items: list[VectorItem]) -> int:
        """Atomic publish: write temp version → validate → switch active → cleanup.

        Default fallback (legacy collections without version fields): delete+upsert.
        """
        if not items:
            # Empty publish deletes the unit.
            self.delete_by_unit(unit_id)
            return 0
        self._validate_items(unit_id, items)
        return self._publish_versioned(unit_id, items)

    def replace_unit(self, unit_id: str, items: list[VectorItem]) -> int:
        return self.publish_unit(unit_id, items)

    @abstractmethod
    def delete_by_unit(self, unit_id: str) -> int: ...

    @abstractmethod
    def search(
        self,
        query_vector: list[float],
        top_k: int = 8,
        *,
        filter: SearchFilter | None = None,
        timeout_sec: float | None = None,
    ) -> list[dict]:
        """Return [{id, unit_id, chunk_index, content, score, version_id, embed_model}].

        ``timeout_sec`` (optional, seconds) is a real I/O deadline handed to the
        backend client (e.g. the Milvus search gRPC deadline) so a pipeline-level
        soft timeout can also bound the underlying blocking call (F-26); the
        in-memory backend completes synchronously and ignores it.
        """
        ...

    def list_active_chunks(self, limit: int = 10000) -> list[dict]:
        """Active-version rows for lexical scoring. Default empty for backends without support."""
        return []

    def get_active_version(self, unit_id: str) -> str | None:
        return None

    def _validate_items(self, unit_id: str, items: list[VectorItem]) -> None:
        models = {i.embed_model for i in items if i.embed_model}
        if len(models) > 1:
            raise PublishValidationError(
                f"mixed embed_model in one publish: {sorted(models)}",
                "PUBLISH_VALIDATE_FAILED",
            )
        for item in items:
            if len(item.content) > MAX_CONTENT_CHARS:
                raise PublishValidationError(
                    f"chunk content length {len(item.content)} exceeds milvus field limit "
                    f"({MAX_CONTENT_CHARS}); split chunk before embed (unit_id={unit_id}, "
                    f"chunk_index={item.chunk_index})",
                    "PUBLISH_VALIDATE_FAILED",
                )

    def _publish_versioned(self, unit_id: str, items: list[VectorItem]) -> int:
        raise NotImplementedError

    def _legacy_replace(self, unit_id: str, items: list[VectorItem]) -> int:
        self.delete_by_unit(unit_id)
        return self.upsert(unit_id, items)


class MemoryVectorStore(VectorStore):
    def __init__(self, dim: int) -> None:
        self._dim = dim
        self._rows: dict[str, dict] = {}  # id -> row
        self._active: dict[str, str] = {}  # unit_id -> version_id
        self._lock = threading.Lock()

    def ensure_collection(self) -> None:
        return

    def get_active_version(self, unit_id: str) -> str | None:
        with self._lock:
            return self._active.get(unit_id)

    def upsert(self, unit_id: str, items: list[VectorItem]) -> int:
        """Legacy path: overwrite without version isolation."""
        for item in items:
            if len(item.embedding) != self._dim:
                raise RuntimeError(f"dim mismatch: {len(item.embedding)} != {self._dim}")
        with self._lock:
            for item in items:
                rid = _stable_id(unit_id, item.chunk_index)
                self._rows[rid] = {
                    "id": rid,
                    "unit_id": unit_id,
                    "chunk_index": item.chunk_index,
                    "content": item.content,
                    "embedding": list(item.embedding),
                    "embed_model": item.embed_model,
                    "version_id": item.version_id or "legacy",
                    "ingest_id": item.ingest_id,
                    "acl_department": item.acl_department or "",
                    "acl_classification": item.acl_classification or "",
                    "heading_path": list(item.heading_path or []),
                }
            self._active[unit_id] = "legacy"
        return len(items)

    def _publish_versioned(self, unit_id: str, items: list[VectorItem]) -> int:
        for item in items:
            if len(item.embedding) != self._dim:
                raise RuntimeError(f"dim mismatch: {len(item.embedding)} != {self._dim}")
        version_id = _new_version_id()
        staged: dict[str, dict] = {}
        for item in items:
            rid = _stable_id(unit_id, item.chunk_index, version_id)
            staged[rid] = {
                "id": rid,
                "unit_id": unit_id,
                "chunk_index": item.chunk_index,
                "content": item.content,
                "embedding": list(item.embedding),
                "embed_model": item.embed_model,
                "version_id": version_id,
                "ingest_id": item.ingest_id,
                "acl_department": item.acl_department or "",
                "acl_classification": item.acl_classification or "",
                "heading_path": list(item.heading_path or []),
            }

        with self._lock:
            prev_active = self._active.get(unit_id)
            # 1) write temp version (not yet searchable)
            self._rows.update(staged)
            # 2) validate
            written = sum(
                1
                for r in self._rows.values()
                if r["unit_id"] == unit_id and r["version_id"] == version_id
            )
            if written != len(items):
                for rid in staged:
                    self._rows.pop(rid, None)
                raise PublishValidationError(
                    f"staged write count mismatch: {written} != {len(items)}"
                )
            # 3) switch active
            self._active[unit_id] = version_id
            # 4) cleanup previous active version only (no full orphan scan)
            if prev_active and prev_active != version_id:
                for rid, row in list(self._rows.items()):
                    if row["unit_id"] == unit_id and row["version_id"] == prev_active:
                        del self._rows[rid]
            logger.info(
                "memory publish unit_id=%s version=%s count=%s prev=%s",
                unit_id,
                version_id,
                len(items),
                prev_active,
            )
        return len(items)

    def delete_by_unit(self, unit_id: str) -> int:
        with self._lock:
            to_del = [rid for rid, row in self._rows.items() if row["unit_id"] == unit_id]
            for rid in to_del:
                del self._rows[rid]
            self._active.pop(unit_id, None)
            return len(to_del)

    def search(
        self,
        query_vector: list[float],
        top_k: int = 8,
        *,
        filter: SearchFilter | None = None,
        timeout_sec: float | None = None,
    ) -> list[dict]:
        # In-memory backend: the call is a synchronous CPU scan, so the deadline
        # is a no-op here (the pipeline's soft timeout still applies around it).
        del timeout_sec

        def cosine(a: list[float], b: list[float]) -> float:
            num = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5 or 1.0
            nb = sum(x * x for x in b) ** 0.5 or 1.0
            return num / (na * nb)

        with self._lock:
            scored = []
            for row in self._rows.values():
                active = self._active.get(row["unit_id"])
                if active and row["version_id"] != active:
                    continue
                if not row_passes_filter(row, filter):
                    continue
                scored.append(
                    {
                        "id": row["id"],
                        "unit_id": row["unit_id"],
                        "chunk_index": row["chunk_index"],
                        "content": row["content"],
                        "score": cosine(query_vector, row["embedding"]),
                        "version_id": row["version_id"],
                        "embed_model": row["embed_model"],
                        "ingest_id": row.get("ingest_id", ""),
                        "acl_department": row.get("acl_department", ""),
                        "acl_classification": row.get("acl_classification", ""),
                        "heading_path": list(row.get("heading_path") or []),
                    }
                )
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[: max(1, top_k)]

    def list_active_chunks(self, limit: int = 10000) -> list[dict]:
        with self._lock:
            out: list[dict] = []
            for row in self._rows.values():
                active = self._active.get(row["unit_id"])
                if active and row["version_id"] != active:
                    continue
                out.append(
                    {
                        "id": row["id"],
                        "unit_id": row["unit_id"],
                        "chunk_index": row["chunk_index"],
                        "content": row["content"],
                        "version_id": row["version_id"],
                        "embed_model": row["embed_model"],
                        "ingest_id": row.get("ingest_id", ""),
                        "acl_department": row.get("acl_department", ""),
                        "acl_classification": row.get("acl_classification", ""),
                        "heading_path": list(row.get("heading_path") or []),
                    }
                )
                if len(out) >= max(1, limit):
                    break
            return out


class MilvusVectorStore(VectorStore):
    """Milvus-backed store with versioned atomic publish.

    Active-version map (R-06) — deployment contract:
      * The ``unit_id -> active version`` map is persisted to ``active_map_path``.
        With the default local path it is **single-writer**: one process owns the
        file. Multiple AI replicas must either point the path at shared storage
        and enable ``retrieve_multi_replica_safe`` (which also forces dense-only,
        because the lexical/sparse indexes stay process-local), or be deployed as
        a single writer.
      * Publishes/deletes for the *same* ``unit_id`` MUST be serialized by a
        single writer. The merge-on-save only prevents lost mappings for
        *distinct* units under concurrent writers; two writers updating the same
        unit are last-writer-wins on the active pointer (there is no cross-process
        per-unit CAS).
      * When a shared path is used, reads refresh on mtime/size change so a peer's
        publish/delete is observed without a restart.

    See ``_cross_process_lock`` for the file-lock limitation (no CAS; strict
    consistency needs a shared CAS store such as MySQL/Redis).
    """

    def __init__(
        self,
        uri: str,
        collection: str,
        dim: int,
        active_map_path: str | None = None,
        embed_require_acl_fields: bool = False,
    ) -> None:
        from pymilvus import MilvusClient

        self._uri = uri
        self._collection = collection
        self._dim = dim
        self._client = MilvusClient(uri=uri)
        self._supports_version = False
        self._supports_embed_model = False
        self._supports_ingest_id = False
        self._supports_acl = False
        self._active: dict[str, str] = {}
        self._active_lock = threading.RLock()
        # R-06: the on-disk active map is a *shared* artefact, not a process-local
        # cache. ``_active_loaded`` is the last file snapshot we read (so a save
        # only re-applies genuinely local changes and never resurrects a unit a
        # peer replica deleted); ``_active_removed`` records local deletions;
        # mtime/size drive change detection so a peer's publish is observed
        # without a restart.
        self._active_loaded: dict[str, str] = {}
        self._active_removed: set[str] = set()
        self._active_map_mtime_ns: int = 0
        self._active_map_size: int = -1
        # Shared active-version store (single-writer default, cross-process safe
        # when pointed at shared storage).
        self._active_map_path = active_map_path
        # R-06: fail-closed lock by default — a lock failure aborts the write so
        # the caller retries instead of silently losing a concurrent update.
        self._active_map_fail_closed = True
        self._active_map_lock_timeout = 5.0
        self._embed_require_acl_fields = bool(embed_require_acl_fields)
        # Publish throughput knobs (optional; tests may leave defaults).
        self._insert_batch_size = 500
        self._count_validate_max = 200
        self._orphan_sweep_on_publish = False
        # R-09: real gRPC deadline for the cold-start `list_active_chunks` scan so
        # a stuck Milvus cannot pin the rebuild thread past the request budget
        # (the outer Future timeout only bounds the *wait*, not the I/O).
        self._list_query_timeout_sec = 0.0

    def ensure_collection(self) -> None:
        from pymilvus import DataType

        required = {"id", "unit_id", "chunk_index", "content", "embedding"}
        if self._client.has_collection(self._collection):
            desc = self._client.describe_collection(self._collection)
            names = {f["name"] for f in desc.get("fields", [])}
            missing = required - names
            if missing:
                raise RuntimeError(
                    f"Milvus collection '{self._collection}' schema incompatible, missing fields: {sorted(missing)}. "
                    f"Use a dedicated collection (e.g. kb_ai_chunks) for this service."
                )
            self._supports_version = "version_id" in names
            self._supports_embed_model = "embed_model" in names
            self._supports_ingest_id = "ingest_id" in names
            self._supports_acl = "acl_department" in names and "acl_classification" in names
            optional = {
                "version_id",
                "embed_model",
                "ingest_id",
                "acl_department",
                "acl_classification",
            }
            missing_optional = optional - names
            if missing_optional:
                self._try_add_optional_fields(DataType, missing_optional)
            self._fail_closed_if_acl_missing()
            self._load_active_map()
            return

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("unit_id", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("content", DataType.VARCHAR, max_length=65535)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self._dim)
        schema.add_field("version_id", DataType.VARCHAR, max_length=64, default_value="")
        schema.add_field("embed_model", DataType.VARCHAR, max_length=128, default_value="")
        schema.add_field("ingest_id", DataType.VARCHAR, max_length=128, default_value="")
        schema.add_field("acl_department", DataType.VARCHAR, max_length=64, default_value="")
        schema.add_field("acl_classification", DataType.VARCHAR, max_length=64, default_value="")
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        self._client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
        )
        self._supports_version = True
        self._supports_embed_model = True
        self._supports_ingest_id = True
        self._supports_acl = True
        self._load_active_map()
        logger.info(
            "milvus collection %s created dim=%s with version fields", self._collection, self._dim
        )

    def _fail_closed_if_acl_missing(self) -> None:
        if getattr(self, "_embed_require_acl_fields", False) and not self._supports_acl:
            raise RuntimeError(
                f"embed_require_acl_fields=true but Milvus collection '{self._collection}' "
                "lacks acl_department/acl_classification columns; "
                "add the columns or set EMBED_REQUIRE_ACL_FIELDS=false"
            )

    def _read_active_map_file(self, path: Path) -> dict[str, str]:
        """Parse the shared file; return {} for missing/corrupt content."""
        try:
            if not path.is_file():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if str(v)}
        except Exception:
            logger.warning("milvus active map read failed path=%s", path, exc_info=True)
        return {}

    def _has_pending_local_changes(self) -> bool:
        with self._active_lock:
            if getattr(self, "_active_removed", None):
                return True
            loaded = getattr(self, "_active_loaded", {})
            return any(loaded.get(u) != v for u, v in self._active.items())

    def _load_active_map(self) -> None:
        path_value = getattr(self, "_active_map_path", None)
        if not path_value:
            return
        path = Path(path_value)
        try:
            data = self._read_active_map_file(path)
            st = path.stat() if path.is_file() else None
        except OSError:
            data, st = {}, None
        with self._active_lock:
            self._active = dict(data)
            self._active_loaded = dict(data)
            self._active_removed = set()
            self._active_map_mtime_ns = st.st_mtime_ns if st else 0
            self._active_map_size = st.st_size if st else -1
        logger.info("milvus active map loaded path=%s units=%s", path, len(data))

    def _refresh_active_map_if_changed(self) -> None:
        """Re-read the shared map when a peer replica wrote it (R-06).

        Without this, a replica that shares ``milvus_active_map_path`` never
        observes another replica's publish/delete until it restarts, so it keeps
        filtering on a stale version. Skipped while a local change is still
        unsaved so a concurrent read cannot clobber it.
        """
        path_value = getattr(self, "_active_map_path", None)
        if not path_value:
            return
        if self._has_pending_local_changes():
            return
        path = Path(path_value)
        try:
            st = path.stat()
        except OSError:
            return
        if st.st_mtime_ns == getattr(self, "_active_map_mtime_ns", 0) and st.st_size == getattr(
            self, "_active_map_size", -1
        ):
            return
        self._load_active_map()

    def _save_active_map(self) -> None:
        """Persist the active map with a read-merge-write under a cross-process lock.

        A naive "serialize my dict" write loses a peer's concurrent publish to a
        *different* unit (last writer wins). Here we re-read the shared file and
        re-apply only this replica's local changes (new/changed units + local
        deletions), so two replicas publishing distinct units both survive. The
        write itself is atomic (temp file + replace).

        R-06: the cross-process lock is fail-closed by default — if it cannot be
        acquired (timeout / missing dir / permission) this raises
        ``ActiveMapLockError`` so the caller (publish/delete) fails and retries,
        instead of writing unlocked and silently dropping a peer's update.

        R-02: a genuine persistence failure (temp write / ``replace`` / ``stat``)
        raises ``ActiveMapPersistenceError`` instead of being logged and
        swallowed — otherwise ``publish_unit`` would delete the previous version
        while the on-disk map still pointed at it. The in-memory snapshot is left
        untouched so the local change is re-applied on the next (retried) save.
        """
        path_value = getattr(self, "_active_map_path", None)
        if not path_value:
            return
        path = Path(path_value)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Snapshot local changes WITHOUT holding the cross-process lock, and
        # never wait on the in-process lock while holding the cross-process
        # lock — a publish (holds the in-process lock) and a delete (holds the
        # cross-process lock) would otherwise deadlock on the two locks.
        with self._active_lock:
            local = dict(self._active)
            loaded = dict(getattr(self, "_active_loaded", {}))
            removed = set(getattr(self, "_active_removed", set()))
        try:
            with _cross_process_lock(
                path,
                timeout=float(getattr(self, "_active_map_lock_timeout", 5.0) or 5.0),
                fail_closed=bool(getattr(self, "_active_map_fail_closed", True)),
            ):
                fresh = self._read_active_map_file(path)
                for unit, ver in local.items():
                    if loaded.get(unit) != ver:
                        fresh[unit] = ver
                for unit in removed:
                    fresh.pop(unit, None)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
                st = path.stat()
        except ActiveMapLockError:
            # Fail-closed: propagate so the caller retries. Do NOT update the
            # in-memory snapshot — the local change is still unsaved and will be
            # re-applied on the next (retried) save.
            get_metrics().inc("retrieve.active_map_lock_failed")
            logger.error(
                "active map save aborted (lock unavailable) path=%s fail_closed=true", path
            )
            raise
        except Exception as exc:
            # R-02: real persistence failure (disk full / read-only / replace
            # failed). Fail closed so publish/delete error out and retry instead
            # of reporting success while the mapping never landed.
            get_metrics().inc("retrieve.active_map_persist_failed")
            logger.error("milvus active map save failed path=%s", path, exc_info=True)
            raise ActiveMapPersistenceError(
                f"active map persistence failed path={path}: {exc}"
            ) from exc
        with self._active_lock:
            self._active = dict(fresh)
            self._active_loaded = dict(fresh)
            self._active_removed = set()
            self._active_map_mtime_ns = st.st_mtime_ns
            self._active_map_size = st.st_size

    def _try_add_optional_fields(self, DataType, missing: set[str]) -> None:
        """Best-effort schema upgrade for legacy collections (pymilvus 2.x/3.x)."""
        try:
            added: set[str] = set()
            for name in missing:
                if name == "version_id":
                    self._add_varchar_field("version_id", 64)
                    self._supports_version = True
                elif name == "embed_model":
                    self._add_varchar_field("embed_model", 128)
                    self._supports_embed_model = True
                elif name == "ingest_id":
                    self._add_varchar_field("ingest_id", 128)
                    self._supports_ingest_id = True
                elif name == "acl_department":
                    self._add_varchar_field("acl_department", 64)
                    added.add("acl_department")
                elif name == "acl_classification":
                    self._add_varchar_field("acl_classification", 64)
                    added.add("acl_classification")
            # ACL requires both columns (pre-existing or successfully added).
            dept_ok = "acl_department" not in missing or "acl_department" in added
            cls_ok = "acl_classification" not in missing or "acl_classification" in added
            if dept_ok and cls_ok:
                self._supports_acl = True
            logger.info("milvus optional fields added: %s", missing)
        except Exception as exc:
            logger.warning(
                "milvus schema upgrade failed for %s missing=%s (%s); legacy publish mode",
                self._collection,
                sorted(missing),
                exc,
            )

    def _add_varchar_field(self, name: str, max_length: int) -> None:
        from pymilvus import DataType

        # pymilvus 3.x: data_type + kwargs (nullable/default_value)
        if hasattr(self._client, "add_collection_field"):
            self._client.add_collection_field(
                collection_name=self._collection,
                field_name=name,
                data_type=DataType.VARCHAR,
                max_length=max_length,
                nullable=True,
                default_value="",
            )
            return
        # older clients
        if hasattr(self._client, "add_schema_field"):
            self._client.add_schema_field(
                collection_name=self._collection,
                field_name=name,
                datatype=DataType.VARCHAR,
                max_length=max_length,
                default_value="",
            )
            return
        raise RuntimeError("MilvusClient has no add_collection_field/add_schema_field")

    def get_active_version(self, unit_id: str) -> str | None:
        self._refresh_active_map_if_changed()
        with self._active_lock:
            return self._active.get(unit_id)

    def upsert(self, unit_id: str, items: list[VectorItem]) -> int:
        """Legacy path: delete then insert (no version isolation)."""
        self._validate_items(unit_id, items)
        data = [self._row_payload(unit_id, item, item.version_id or "legacy") for item in items]
        self.delete_by_unit(unit_id)
        if not data:
            return 0
        self._client.insert(collection_name=self._collection, data=data)
        try:
            self._client.flush(self._collection)
        except Exception:
            logger.debug("milvus flush skipped/failed", exc_info=True)
        return len(data)

    def _row_payload(self, unit_id: str, item: VectorItem, version_id: str) -> dict:
        if len(item.embedding) != self._dim:
            raise RuntimeError(f"dim mismatch: {len(item.embedding)} != {self._dim}")
        if len(item.content) > MAX_CONTENT_CHARS:
            raise PublishValidationError(
                f"chunk content length {len(item.content)} exceeds milvus field limit "
                f"({MAX_CONTENT_CHARS}); split chunk before embed (unit_id={unit_id}, "
                f"chunk_index={item.chunk_index})",
                "PUBLISH_VALIDATE_FAILED",
            )
        row = {
            "id": _stable_id(unit_id, item.chunk_index, version_id),
            "unit_id": unit_id,
            "chunk_index": item.chunk_index,
            "content": item.content,
            "embedding": item.embedding,
        }
        if self._supports_version:
            row["version_id"] = version_id
        if self._supports_embed_model:
            row["embed_model"] = item.embed_model
        if self._supports_ingest_id:
            row["ingest_id"] = item.ingest_id or ""
        if getattr(self, "_supports_acl", False):
            row["acl_department"] = item.acl_department or ""
            row["acl_classification"] = item.acl_classification or ""
        return row

    def _version_has_rows(self, unit_id: str, version_id: str) -> bool:
        """Cheap existence check for large publishes (avoid full count scan)."""
        safe_id = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        safe_ver = version_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            res = self._client.query(
                collection_name=self._collection,
                filter=f'unit_id == "{safe_id}" and version_id == "{safe_ver}"',
                output_fields=["id"],
                limit=1,
            )
            return bool(res)
        except TypeError:
            # Older clients without limit kwarg
            res = self._client.query(
                collection_name=self._collection,
                filter=f'unit_id == "{safe_id}" and version_id == "{safe_ver}"',
                output_fields=["id"],
            )
            return bool(res)
        except Exception as exc:
            raise PublishValidationError(f"version sample query failed: {exc}") from exc

    def _count_version(self, unit_id: str, version_id: str) -> int:
        safe_id = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        safe_ver = version_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            res = self._client.query(
                collection_name=self._collection,
                filter=f'unit_id == "{safe_id}" and version_id == "{safe_ver}"',
                output_fields=["id"],
            )
            return len(res or [])
        except Exception as exc:
            raise PublishValidationError(f"version count query failed: {exc}") from exc

    def _delete_version(self, unit_id: str, version_id: str) -> int:
        safe_id = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        safe_ver = version_id.replace("\\", "\\\\").replace('"', '\\"')
        res = self._client.delete(
            collection_name=self._collection,
            filter=f'unit_id == "{safe_id}" and version_id == "{safe_ver}"',
        )
        if isinstance(res, dict):
            return int(res.get("delete_count") or 0)
        return 0

    def _delete_non_active(self, unit_id: str, active_version: str) -> None:
        """Delete orphan versions. Milvus expr may not honor `!=` reliably — enumerate then `==`."""
        safe_id = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            rows = self._client.query(
                collection_name=self._collection,
                filter=f'unit_id == "{safe_id}"',
                output_fields=["version_id"],
            )
        except Exception as exc:
            raise PublishValidationError(f"orphan version query failed: {exc}") from exc
        old_versions = {
            str(r.get("version_id") or "")
            for r in (rows or [])
            if str(r.get("version_id") or "") and str(r.get("version_id")) != active_version
        }
        for ver in old_versions:
            self._delete_version(unit_id, ver)
        try:
            self._client.flush(self._collection)
        except Exception:
            logger.debug("milvus flush after cleanup skipped", exc_info=True)

    def _publish_versioned(self, unit_id: str, items: list[VectorItem]) -> int:
        if not (self._supports_version and self._supports_embed_model):
            logger.warning(
                "milvus collection %s lacks version fields; legacy delete+upsert for unit_id=%s",
                self._collection,
                unit_id,
            )
            return self.upsert(unit_id, items)

        version_id = _new_version_id()
        data = [self._row_payload(unit_id, item, version_id) for item in items]
        insert_batch = max(1, int(getattr(self, "_insert_batch_size", 500) or 500))
        count_validate_max = int(getattr(self, "_count_validate_max", 200) or 200)
        orphan_sweep = bool(getattr(self, "_orphan_sweep_on_publish", False))
        with self._active_lock:
            prev = self._active.get(unit_id)
            try:
                # 1) write temp version in batches (single flush at end)
                for i in range(0, len(data), insert_batch):
                    self._client.insert(
                        collection_name=self._collection, data=data[i : i + insert_batch]
                    )
                    get_metrics().inc("milvus.publish_batch")
                try:
                    self._client.flush(self._collection)
                except Exception:
                    logger.debug("milvus flush skipped", exc_info=True)
                # 2) validate: full count for small units; sample existence for large
                if len(items) <= count_validate_max:
                    count = self._count_version(unit_id, version_id)
                    if count != len(items):
                        raise PublishValidationError(
                            f"milvus staged count {count} != expected {len(items)}"
                        )
                else:
                    if not self._version_has_rows(unit_id, version_id):
                        raise PublishValidationError(
                            f"milvus staged version empty after insert expected={len(items)}"
                        )
                    get_metrics().inc("milvus.publish_count_skipped")
                # 3) switch active and PERSIST the mapping *before* removing the
                # previous version (R-02). If persistence fails, ``_save_active_map``
                # raises and the outer handler rolls back below — the old version
                # is never deleted, so a retry stays consistent.
                self._active[unit_id] = version_id
                self._save_active_map()
                # 4) cleanup previous active only (fast path); optional full orphan sweep
                try:
                    if prev and prev != version_id:
                        self._delete_version(unit_id, prev)
                    if orphan_sweep:
                        self._delete_non_active(unit_id, version_id)
                except Exception:
                    logger.exception("milvus cleanup old versions failed unit_id=%s", unit_id)
                try:
                    self._client.flush(self._collection)
                except Exception:
                    logger.debug("milvus flush after cleanup skipped", exc_info=True)
                logger.info(
                    "milvus publish unit_id=%s version=%s count=%s prev=%s",
                    unit_id,
                    version_id,
                    len(items),
                    prev,
                )
                return len(items)
            except Exception as exc:
                # rollback temp version; keep previous active
                try:
                    self._delete_version(unit_id, version_id)
                except Exception:
                    logger.exception(
                        "milvus rollback delete failed unit_id=%s version=%s", unit_id, version_id
                    )
                if prev:
                    self._active[unit_id] = prev
                else:
                    self._active.pop(unit_id, None)
                # R-06/R-02: the rollback save is best-effort — never let a lock
                # or persistence failure here mask the original publish error.
                try:
                    self._save_active_map()
                except (ActiveMapLockError, ActiveMapPersistenceError):
                    logger.error("milvus rollback active-map save failed unit_id=%s", unit_id)
                # R-02: a persistence/lock failure must surface as itself (not be
                # re-wrapped) so the caller sees a retryable storage error.
                if isinstance(exc, (ActiveMapLockError, ActiveMapPersistenceError)):
                    raise
                if isinstance(exc, PublishValidationError):
                    raise
                raise PublishValidationError(
                    f"milvus publish failed and rolled back: {exc}"
                ) from exc

    def delete_by_unit(self, unit_id: str) -> int:
        """Delete vectors for a unit and durably drop its active-map entry (R-02).

        Idempotent state machine: step 1 (vector delete) and step 2 (mapping
        persist) are individually retryable. The vector delete runs first; a
        mapping-persist failure then raises ``ActiveMapPersistenceError`` (or
        ``ActiveMapLockError``) so the caller retries the whole call. Re-running
        the delete is safe — Milvus delete and the map removal are both
        idempotent — and a successful retry lands the mapping.
        """
        safe_id = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            res = self._client.delete(
                collection_name=self._collection,
                filter=f'unit_id == "{safe_id}"',
            )
        except Exception as exc:
            logger.exception("milvus delete_by_unit failed unit_id=%s", unit_id)
            raise RuntimeError(f"milvus delete failed for unit_id={unit_id}: {exc}") from exc
        # Vectors are gone; now persist the mapping removal. A failure here is a
        # retryable persistence error (the caller must not treat delete as done).
        with self._active_lock:
            self._active.pop(unit_id, None)
            self._active_removed.add(unit_id)
        self._save_active_map()
        if isinstance(res, dict):
            return int(res.get("delete_count") or 0)
        return 0

    @staticmethod
    def _escape_str(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _build_filter_expr(self, flt: SearchFilter | None) -> str | None:
        if flt is None:
            return None
        # Empty allowlist is deny-all; cannot express in Milvus expr — return impossible clause.
        if flt.unit_ids is not None and len(flt.unit_ids) == 0:
            return 'unit_id == "__deny_all__"'
        clauses: list[str] = []
        if flt.unit_ids:
            ids = ", ".join(f'"{self._escape_str(u)}"' for u in flt.unit_ids)
            clauses.append(f"unit_id in [{ids}]")
        if flt.deny_unit_ids:
            ids = ", ".join(f'"{self._escape_str(u)}"' for u in flt.deny_unit_ids)
            clauses.append(f"unit_id not in [{ids}]")
        dept = (flt.department or "").strip()
        if dept and getattr(self, "_supports_acl", False):
            clauses.append(f'acl_department == "{self._escape_str(dept)}"')
        classification = (flt.classification or "").strip()
        if classification and getattr(self, "_supports_acl", False):
            clauses.append(f'acl_classification == "{self._escape_str(classification)}"')
        if not clauses:
            return None
        return " and ".join(clauses)

    def search(
        self,
        query_vector: list[float],
        top_k: int = 8,
        *,
        filter: SearchFilter | None = None,
        timeout_sec: float | None = None,
    ) -> list[dict]:
        if len(query_vector) != self._dim:
            raise RuntimeError(f"query dim mismatch: {len(query_vector)} != {self._dim}")
        output_fields = ["unit_id", "chunk_index", "content"]
        if self._supports_version:
            output_fields.append("version_id")
        if self._supports_embed_model:
            output_fields.append("embed_model")
        if self._supports_ingest_id:
            output_fields.append("ingest_id")
        if getattr(self, "_supports_acl", False):
            output_fields.extend(["acl_department", "acl_classification"])
        kwargs: dict[str, Any] = {
            "collection_name": self._collection,
            "data": [query_vector],
            "limit": max(1, top_k),
            "output_fields": output_fields,
            "search_params": {"metric_type": "COSINE"},
        }
        # F-26: propagate a REAL client deadline. Without it a stuck Milvus node
        # keeps the gRPC call (and its pooled thread) alive long after the
        # pipeline's soft timeout fired.
        rpc_timeout = float(timeout_sec or 0)
        if rpc_timeout > 0:
            kwargs["timeout"] = rpc_timeout
        expr = self._build_filter_expr(filter)
        if expr:
            kwargs["filter"] = expr
        # R-06: pick up a peer replica's publish/delete before filtering rows, so
        # a shared active map never serves a stale version without a restart.
        self._refresh_active_map_if_changed()
        res = self._client.search(**kwargs)
        hits = res[0] if res else []
        out: list[dict] = []
        for hit in hits:
            entity = hit.get("entity") or hit
            unit = str(entity.get("unit_id") or "")
            ver = str(entity.get("version_id") or "")
            with self._active_lock:
                active = self._active.get(unit)
            if active and ver and ver != active:
                continue
            row = {
                "id": str(hit.get("id") or entity.get("id") or ""),
                "unit_id": unit,
                "chunk_index": int(entity.get("chunk_index") or 0),
                "content": str(entity.get("content") or ""),
                "score": float(hit.get("distance") or hit.get("score") or 0.0),
                "version_id": ver,
                "embed_model": str(entity.get("embed_model") or ""),
                "ingest_id": str(entity.get("ingest_id") or ""),
                "acl_department": str(entity.get("acl_department") or ""),
                "acl_classification": str(entity.get("acl_classification") or ""),
            }
            if not row_passes_filter(row, filter):
                continue
            out.append(row)
        return out

    # Milvus caps a single query window at 16384 (offset+limit). A collection can
    # hold far more rows once several versions coexist, so a single query silently
    # truncates and drops whole units. Page by primary key (keyset) instead.
    _QUERY_WINDOW = 16384

    def list_active_chunks(self, limit: int = 10000) -> list[dict]:
        # `id` is required as the keyset cursor for pagination.
        output_fields = ["id", "unit_id", "chunk_index", "content", "version_id"]
        # ACL columns must be requested whenever the collection carries them:
        # the lexical/list channel filters rows by acl_department /
        # acl_classification downstream, and an unrequested output field reads
        # back as "" — the filter then drops every row (silent ACL wipe). Mirror
        # search() above.
        if getattr(self, "_supports_acl", False):
            output_fields.extend(["acl_department", "acl_classification"])
        want = max(1, int(limit))
        # R-06: refresh the shared map first so a peer's publish/delete is seen.
        self._refresh_active_map_if_changed()
        # Build a server-side version filter so paging only walks rows we keep.
        # Without it, the first `limit` rows are mostly stale versions and real
        # active rows past that point are dropped (units vanish from the index).
        with self._active_lock:
            active_map = dict(self._active)
        version_clauses = [
            f'version_id == "{self._escape_str(v)}"' for v in sorted(set(active_map.values())) if v
        ]
        server_filter = " or ".join(version_clauses) if version_clauses else None
        rows: list[dict] = []
        try:
            rows = self._query_all_rows(output_fields, want, base_filter=server_filter)
        except Exception:
            logger.warning("milvus list_active_chunks query failed", exc_info=True)
            return []
        out: list[dict] = []
        with self._active_lock:
            for r in rows or []:
                unit = str(r.get("unit_id") or "")
                ver = str(r.get("version_id") or "")
                active = self._active.get(unit)
                if active and ver and ver != active:
                    continue
                out.append(
                    {
                        "id": str(r.get("id") or ""),
                        "unit_id": unit,
                        "chunk_index": int(r.get("chunk_index") or 0),
                        "content": str(r.get("content") or ""),
                        "version_id": ver,
                        "acl_department": str(r.get("acl_department") or ""),
                        "acl_classification": str(r.get("acl_classification") or ""),
                    }
                )
        return out

    def _query_page(self, output_fields: list[str], flt: str, page: int) -> list[dict]:
        """One keyset page with a real client deadline when configured (R-09)."""
        kwargs: dict[str, Any] = {
            "collection_name": self._collection,
            "filter": flt,
            "output_fields": output_fields,
            "limit": page,
        }
        timeout_sec = float(getattr(self, "_list_query_timeout_sec", 0) or 0)
        if timeout_sec <= 0:
            return self._client.query(**kwargs)
        kwargs["timeout"] = timeout_sec
        try:
            return self._client.query(**kwargs)
        except TypeError:
            # Older client without a `timeout` kwarg: fall back to no deadline.
            kwargs.pop("timeout", None)
            return self._client.query(**kwargs)

    def _query_all_rows(
        self, output_fields: list[str], want: int, *, base_filter: str | None = None
    ) -> list[dict]:
        """Fetch up to `want` rows using keyset pagination on the primary key.

        A single `query(limit=N)` is rejected once N exceeds the server window
        (16384) and, worse, silently returns a truncated slice when the caller
        caps the limit. Paging by `id > '<last>'` keeps every page inside the
        window and guarantees full coverage. Milvus PK here is VarChar, so the
        cursor is compared as an escaped string literal, not an integer.
        `base_filter` (e.g. a version_id whitelist) narrows the scan so paging
        does not burn the budget on rows the caller will discard anyway.
        """
        page = min(self._QUERY_WINDOW, max(1, want))
        rows: list[dict] = []
        cursor: str | None = None
        while len(rows) < want:
            clauses = []
            if base_filter:
                clauses.append(f"({base_filter})")
            if cursor is not None:
                clauses.append(f'id > "{self._escape_str(cursor)}"')
            flt = " and ".join(clauses)
            try:
                batch = self._query_page(output_fields, flt, page)
            except Exception:
                if cursor is None:
                    raise
                logger.warning("milvus list_active_chunks paging stopped", exc_info=True)
                break
            if not batch:
                break
            rows.extend(batch)
            ids = [str(r.get("id") or "") for r in batch]
            if not ids or not all(ids):
                break
            cursor = max(ids)
            if len(batch) < page:
                break
        return rows[:want]


def create_vector_store(cfg: Settings) -> VectorStore:
    kind = (cfg.vector_store or "memory").lower()
    if kind == "memory":
        return MemoryVectorStore(dim=cfg.embed_dim)
    if kind == "milvus":
        store = MilvusVectorStore(
            uri=cfg.milvus_uri,
            collection=cfg.milvus_collection,
            dim=cfg.embed_dim,
            active_map_path=cfg.milvus_active_map_path or None,
            embed_require_acl_fields=bool(cfg.embed_require_acl_fields),
        )
        store._insert_batch_size = int(getattr(cfg, "milvus_insert_batch_size", 500) or 500)
        store._count_validate_max = int(getattr(cfg, "milvus_count_validate_max", 200) or 200)
        store._orphan_sweep_on_publish = bool(getattr(cfg, "milvus_orphan_sweep_on_publish", False))
        store._list_query_timeout_sec = float(getattr(cfg, "retrieve_store_timeout_sec", 0) or 0)
        # R-06: fail-closed cross-process lock unless explicitly opted out.
        store._active_map_fail_closed = bool(getattr(cfg, "retrieve_active_map_fail_closed", True))
        return store
    raise RuntimeError(f"unknown VECTOR_STORE: {kind}")
