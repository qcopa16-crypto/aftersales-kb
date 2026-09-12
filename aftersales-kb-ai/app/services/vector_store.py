"""Vector store backends: Milvus and in-memory. Supports versioned atomic publish."""

from __future__ import annotations

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import Settings

logger = logging.getLogger(__name__)

# Legacy content cap (Milvus VARCHAR practical guard).
MAX_CONTENT_CHARS = 60000


class PublishValidationError(RuntimeError):
    def __init__(self, message: str, error_code: str = "PUBLISH_VALIDATE_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class VectorItem:
    chunk_index: int
    content: str
    embedding: list[float]
    embed_model: str = ""
    version_id: str = ""
    ingest_id: str = ""


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
    def search(self, query_vector: list[float], top_k: int = 8) -> list[dict]:
        """Return [{id, unit_id, chunk_index, content, score, version_id, embed_model}]."""
        ...

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
            }

        with self._lock:
            prev_active = self._active.get(unit_id)
            # 1) write temp version (not yet searchable)
            self._rows.update(staged)
            # 2) validate
            written = sum(1 for r in self._rows.values() if r["unit_id"] == unit_id and r["version_id"] == version_id)
            if written != len(items):
                for rid in staged:
                    self._rows.pop(rid, None)
                raise PublishValidationError(
                    f"staged write count mismatch: {written} != {len(items)}"
                )
            # 3) switch active
            self._active[unit_id] = version_id
            # 4) cleanup old versions
            for rid, row in list(self._rows.items()):
                if row["unit_id"] == unit_id and row["version_id"] != version_id:
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

    def search(self, query_vector: list[float], top_k: int = 8) -> list[dict]:
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
                    }
                )
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[: max(1, top_k)]


class MilvusVectorStore(VectorStore):
    def __init__(self, uri: str, collection: str, dim: int) -> None:
        from pymilvus import MilvusClient

        self._uri = uri
        self._collection = collection
        self._dim = dim
        self._client = MilvusClient(uri=uri)
        self._supports_version = False
        self._supports_embed_model = False
        self._supports_ingest_id = False
        self._active: dict[str, str] = {}
        self._active_lock = threading.Lock()

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
            optional = {"version_id", "embed_model", "ingest_id"}
            missing_optional = optional - names
            if missing_optional:
                self._try_add_optional_fields(DataType, missing_optional)
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
        logger.info("milvus collection %s created dim=%s with version fields", self._collection, self._dim)

    def _try_add_optional_fields(self, DataType, missing: set[str]) -> None:
        """Best-effort schema upgrade for legacy collections (pymilvus 2.x/3.x)."""
        try:
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
        return row

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
        with self._active_lock:
            prev = self._active.get(unit_id)
            try:
                # 1) write temp version
                self._client.insert(collection_name=self._collection, data=data)
                try:
                    self._client.flush(self._collection)
                except Exception:
                    logger.debug("milvus flush skipped", exc_info=True)
                # 2) validate
                count = self._count_version(unit_id, version_id)
                if count != len(items):
                    raise PublishValidationError(
                        f"milvus staged count {count} != expected {len(items)}"
                    )
                # 3) switch active
                self._active[unit_id] = version_id
                # 4) cleanup old / non-active
                try:
                    self._delete_non_active(unit_id, version_id)
                except Exception:
                    logger.exception("milvus cleanup old versions failed unit_id=%s", unit_id)
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
                    logger.exception("milvus rollback delete failed unit_id=%s version=%s", unit_id, version_id)
                if prev:
                    self._active[unit_id] = prev
                else:
                    self._active.pop(unit_id, None)
                if isinstance(exc, PublishValidationError):
                    raise
                raise PublishValidationError(f"milvus publish failed and rolled back: {exc}") from exc

    def delete_by_unit(self, unit_id: str) -> int:
        safe_id = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            res = self._client.delete(
                collection_name=self._collection,
                filter=f'unit_id == "{safe_id}"',
            )
            with self._active_lock:
                self._active.pop(unit_id, None)
            if isinstance(res, dict):
                return int(res.get("delete_count") or 0)
            return 0
        except Exception as exc:
            logger.exception("milvus delete_by_unit failed unit_id=%s", unit_id)
            raise RuntimeError(f"milvus delete failed for unit_id={unit_id}: {exc}") from exc

    def search(self, query_vector: list[float], top_k: int = 8) -> list[dict]:
        if len(query_vector) != self._dim:
            raise RuntimeError(f"query dim mismatch: {len(query_vector)} != {self._dim}")
        output_fields = ["unit_id", "chunk_index", "content"]
        if self._supports_version:
            output_fields.append("version_id")
        if self._supports_embed_model:
            output_fields.append("embed_model")
        if self._supports_ingest_id:
            output_fields.append("ingest_id")
        res = self._client.search(
            collection_name=self._collection,
            data=[query_vector],
            limit=max(1, top_k),
            output_fields=output_fields,
            search_params={"metric_type": "COSINE"},
        )
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
            out.append(
                {
                    "id": str(hit.get("id") or entity.get("id") or ""),
                    "unit_id": unit,
                    "chunk_index": int(entity.get("chunk_index") or 0),
                    "content": str(entity.get("content") or ""),
                    "score": float(hit.get("distance") or hit.get("score") or 0.0),
                    "version_id": ver,
                    "embed_model": str(entity.get("embed_model") or ""),
                    "ingest_id": str(entity.get("ingest_id") or ""),
                }
            )
        return out


def create_vector_store(cfg: Settings) -> VectorStore:
    kind = (cfg.vector_store or "memory").lower()
    if kind == "memory":
        return MemoryVectorStore(dim=cfg.embed_dim)
    if kind == "milvus":
        return MilvusVectorStore(
            uri=cfg.milvus_uri,
            collection=cfg.milvus_collection,
            dim=cfg.embed_dim,
        )
    raise RuntimeError(f"unknown VECTOR_STORE: {kind}")
