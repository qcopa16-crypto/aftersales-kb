"""Vector store backends: Milvus and in-memory for tests."""

from __future__ import annotations

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class VectorItem:
    chunk_index: int
    content: str
    embedding: list[float]


def _stable_id(unit_id: str, chunk_index: int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{unit_id}::{chunk_index}").hex


class VectorStore(ABC):
    @abstractmethod
    def ensure_collection(self) -> None: ...

    @abstractmethod
    def upsert(self, unit_id: str, items: list[VectorItem]) -> int: ...

    @abstractmethod
    def delete_by_unit(self, unit_id: str) -> int: ...

    @abstractmethod
    def search(self, query_vector: list[float], top_k: int = 8) -> list[dict]:
        """Return [{id, unit_id, chunk_index, content, score}] sorted by score desc."""
        ...


class MemoryVectorStore(VectorStore):
    def __init__(self, dim: int) -> None:
        self._dim = dim
        self._rows: dict[str, dict] = {}  # id -> row
        self._lock = threading.Lock()

    def ensure_collection(self) -> None:
        return

    def upsert(self, unit_id: str, items: list[VectorItem]) -> int:
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
                }
        return len(items)

    def delete_by_unit(self, unit_id: str) -> int:
        with self._lock:
            to_del = [rid for rid, row in self._rows.items() if row["unit_id"] == unit_id]
            for rid in to_del:
                del self._rows[rid]
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
                scored.append(
                    {
                        "id": row["id"],
                        "unit_id": row["unit_id"],
                        "chunk_index": row["chunk_index"],
                        "content": row["content"],
                        "score": cosine(query_vector, row["embedding"]),
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
            return
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("unit_id", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("content", DataType.VARCHAR, max_length=65535)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self._dim)
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
        logger.info("milvus collection %s created dim=%s", self._collection, self._dim)

    def upsert(self, unit_id: str, items: list[VectorItem]) -> int:
        data = []
        for item in items:
            if len(item.embedding) != self._dim:
                raise RuntimeError(f"dim mismatch: {len(item.embedding)} != {self._dim}")
            content = item.content
            if len(content) > 60000:
                content = content[:60000]
            data.append(
                {
                    "id": _stable_id(unit_id, item.chunk_index),
                    "unit_id": unit_id,
                    "chunk_index": item.chunk_index,
                    "content": content,
                    "embedding": item.embedding,
                }
            )
        # delete old then insert (upsert semantics)
        self.delete_by_unit(unit_id)
        self._client.insert(collection_name=self._collection, data=data)
        try:
            self._client.flush(self._collection)
        except Exception:
            # flush is best-effort for immediate query visibility
            logger.debug("milvus flush skipped/failed", exc_info=True)
        return len(data)

    def delete_by_unit(self, unit_id: str) -> int:
        # Escape double quotes in unit_id to keep the filter expression valid.
        safe_id = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            res = self._client.delete(
                collection_name=self._collection,
                filter=f'unit_id == "{safe_id}"',
            )
            if isinstance(res, dict):
                return int(res.get("delete_count") or 0)
            return 0
        except Exception as exc:
            # Re-raise so callers can fail the request instead of claiming success.
            logger.exception("milvus delete_by_unit failed unit_id=%s", unit_id)
            raise RuntimeError(f"milvus delete failed for unit_id={unit_id}: {exc}") from exc

    def search(self, query_vector: list[float], top_k: int = 8) -> list[dict]:
        if len(query_vector) != self._dim:
            raise RuntimeError(f"query dim mismatch: {len(query_vector)} != {self._dim}")
        res = self._client.search(
            collection_name=self._collection,
            data=[query_vector],
            limit=max(1, top_k),
            output_fields=["unit_id", "chunk_index", "content"],
            search_params={"metric_type": "COSINE"},
        )
        hits = res[0] if res else []
        out: list[dict] = []
        for hit in hits:
            entity = hit.get("entity") or hit
            out.append(
                {
                    "id": str(hit.get("id") or entity.get("id") or ""),
                    "unit_id": str(entity.get("unit_id") or ""),
                    "chunk_index": int(entity.get("chunk_index") or 0),
                    "content": str(entity.get("content") or ""),
                    "score": float(hit.get("distance") or hit.get("score") or 0.0),
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
