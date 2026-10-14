"""Process-local sparse inverted index over BGE-M3 lexical_weights.

Mirrors LexicalIndex (single-writer, lock, replace/delete/sync) but indexes
term→weight sparse vectors instead of BM25 term frequencies. Sparse weights are
computed by the embedder at embed time and stored here; a cold-start rebuild
re-encodes active chunks via `sync_from_store`.

Multi-replica correctness is not a goal (same as the lexical index).
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ai.retrieve.store import SearchFilter, row_passes_filter

logger = logging.getLogger(__name__)


@dataclass
class _SparseDoc:
    chunk_id: str
    unit_id: str
    chunk_index: int
    content: str
    version_id: str
    weights: dict[str, float] = field(default_factory=dict)
    heading_path: list[str] = field(default_factory=list)
    acl_department: str = ""
    acl_classification: str = ""


class SparseIndex:
    """Term→weight inverted index; search = dot product over shared terms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._docs: dict[str, _SparseDoc] = {}
        self._by_unit: dict[str, set[str]] = defaultdict(set)
        self._postings: dict[str, set[str]] = defaultdict(set)
        # Readiness gate: False until a full sync completes. Lets the retrieve
        # path skip the index (graceful degrade) instead of triggering an
        # unbounded in-request rebuild on cold start.
        self._ready = False

    def is_ready(self) -> bool:
        """True once a full sync has completed (even for an empty corpus)."""
        return self._ready

    def mark_ready(self) -> None:
        self._ready = True

    def _doc_key(self, unit_id: str, chunk_id: str, version_id: str) -> str:
        return f"{unit_id}::{chunk_id}::{version_id}"

    def replace_unit(self, unit_id: str, version_id: str, docs: list[dict]) -> int:
        """Atomically replace all sparse docs for a unit (active version only)."""
        prepared: list[tuple[str, _SparseDoc]] = []
        for d in docs:
            chunk_id = str(d.get("id") or d.get("chunk_id") or d.get("chunk_index") or "")
            weights = {
                str(k): float(v) for k, v in (d.get("weights") or {}).items() if float(v) != 0.0
            }
            prepared.append(
                (
                    self._doc_key(unit_id, chunk_id, version_id),
                    _SparseDoc(
                        chunk_id=chunk_id,
                        unit_id=unit_id,
                        chunk_index=int(d.get("chunk_index") or 0),
                        content=str(d.get("content") or ""),
                        version_id=version_id,
                        weights=weights,
                        heading_path=list(d.get("heading_path") or []),
                        acl_department=str(d.get("acl_department") or ""),
                        acl_classification=str(d.get("acl_classification") or ""),
                    ),
                )
            )
        with self._lock:
            for key in self._by_unit.pop(unit_id, set()):
                doc = self._docs.pop(key, None)
                if doc:
                    for term in doc.weights:
                        post = self._postings.get(term)
                        if post is not None:
                            post.discard(key)
                            if not post:
                                self._postings.pop(term, None)
            for key, doc in prepared:
                self._docs[key] = doc
                self._by_unit[unit_id].add(key)
                for term in doc.weights:
                    self._postings[term].add(key)
            return len(prepared)

    def delete_unit(self, unit_id: str) -> int:
        with self._lock:
            keys = self._by_unit.pop(unit_id, set())
            for key in keys:
                doc = self._docs.pop(key, None)
                if doc:
                    for term in doc.weights:
                        post = self._postings.get(term)
                        if post is not None:
                            post.discard(key)
                            if not post:
                                self._postings.pop(term, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()
            self._by_unit.clear()
            self._postings.clear()
        # An emptied index is no longer ready; the next sync must rebuild it.
        self._ready = False

    def doc_count(self) -> int:
        with self._lock:
            return len(self._docs)

    def search(
        self,
        query_weights: dict[str, float],
        top_k: int = 8,
        *,
        filter: SearchFilter | None = None,
        max_docs: int | None = None,
    ) -> list[dict]:
        qw = {str(k): float(v) for k, v in (query_weights or {}).items() if float(v) != 0.0}
        if not qw:
            return []
        with self._lock:
            cand_keys: set[str] = set()
            for term in qw:
                cand_keys |= self._postings.get(term, set())
            cap = int(max_docs or 0)
            if cap > 0 and len(cand_keys) > cap:
                cand_keys = set(sorted(cand_keys)[:cap])
                logger.warning("sparse posting cap applied candidates>=%s", cap)
            docs = [self._docs[k] for k in cand_keys if k in self._docs]
        scored: list[dict] = []
        for doc in docs:
            row = {
                "id": doc.chunk_id,
                "unit_id": doc.unit_id,
                "chunk_index": doc.chunk_index,
                "content": doc.content,
                "version_id": doc.version_id,
                "heading_path": list(doc.heading_path),
                "acl_department": doc.acl_department,
                "acl_classification": doc.acl_classification,
            }
            if not row_passes_filter(row, filter):
                continue
            score = sum(qw.get(t, 0.0) * w for t, w in doc.weights.items() if t in qw)
            if score <= 0.0:
                continue
            row["score"] = float(score)
            scored.append(row)
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[: max(1, top_k)]

    def sync_from_store(self, store: Any, embedder: Any, limit: int | None = None) -> int:
        """Rebuild by re-encoding active chunks' sparse weights. No-op if the
        embedder cannot emit sparse weights (cloud/fake)."""
        if not getattr(embedder, "supports_sparse", False):
            # Nothing to index for this embedder: the index is trivially ready so
            # callers/preheat do not retry an unsupported path forever.
            self._ready = True
            return 0
        rows = store.list_active_chunks(limit=limit or 50000)
        by_unit: dict[str, list[dict]] = defaultdict(list)
        versions: dict[str, str] = {}
        for row in rows:
            unit = str(row.get("unit_id") or "")
            by_unit[unit].append(row)
            versions[unit] = str(row.get("version_id") or "legacy")
        with self._lock:
            self._docs.clear()
            self._by_unit.clear()
            self._postings.clear()
        count = 0
        for unit, docs in by_unit.items():
            texts = [str(d.get("content") or "") for d in docs]
            weights = embedder.encode_sparse(texts)
            payload = [
                {
                    "id": str(d.get("id") or ""),
                    "chunk_index": int(d.get("chunk_index") or 0),
                    "content": str(d.get("content") or ""),
                    "heading_path": list(d.get("heading_path") or []),
                    "acl_department": str(d.get("acl_department") or ""),
                    "acl_classification": str(d.get("acl_classification") or ""),
                    "weights": w,
                }
                for d, w in zip(docs, weights)
            ]
            count += self.replace_unit(unit, versions.get(unit, "legacy"), payload)
        self._ready = True
        logger.info("sparse index synced from store docs=%s units=%s", count, len(by_unit))
        return count


_default_index: SparseIndex | None = None
_default_lock = threading.Lock()


def get_sparse_index() -> SparseIndex:
    global _default_index
    with _default_lock:
        if _default_index is None:
            _default_index = SparseIndex()
        return _default_index


def reset_sparse_index_for_tests() -> None:
    """Drop the process-global sparse index so each test starts unready."""
    global _default_index
    with _default_lock:
        _default_index = None
