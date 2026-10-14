"""In-process lexical BM25 over active chunks. No third-party search engines."""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

from ai.retrieve.store import SearchFilter, VectorStore, row_passes_filter

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RE = re.compile(r"[一-鿿]+")
# Error-code canonicalization: "E-05" / "E05" / "E 05" all anchor to "e05".
_CODE_RE = re.compile(r"\b([A-Za-z]{1,4})[- ]?(\d{1,4})\b")

# Bounded LRU for tokenization (P8). Explicit, hard cap so the process cannot
# grow an unbounded token cache across a long-lived server.
#
# Cache key is the *text alone*: `tokenize` is a pure, model-independent
# transform (regex only) — the same text always yields the same tokens for every
# embedder / encoder. There is therefore no "cross-model" contamination risk
# here; keying on a model id would only fragment the cache for no benefit. The
# model-dimension-in-key rule applies to the *vector* caches (dense + sparse),
# which are keyed on (embed_model_id, embed_dim, question_hash).
_TOKENIZE_CACHE_MAX = 8192


@lru_cache(maxsize=_TOKENIZE_CACHE_MAX)
def _tokenize_cached(text: str) -> tuple[str, ...]:
    """Cached core; returns an immutable tuple so callers can never mutate it."""
    if not text:
        return ()
    tokens: list[str] = []
    lowered = text.lower()
    for m in _TOKEN_RE.finditer(lowered):
        tokens.append(m.group(0))
    # Canonical error-code tokens (joined, lowercased) for stronger exact match.
    for m in _CODE_RE.finditer(lowered):
        tokens.append(m.group(1) + m.group(2))
    for m in _CJK_RE.finditer(text):
        s = m.group(0)
        for ch in s:
            tokens.append(ch)
        for i in range(len(s) - 1):
            tokens.append(s[i : i + 2])
    return tuple(tokens)


def tokenize(text: str) -> list[str]:
    """English/number tokens + Chinese unigrams/bigrams. No external segmenter.

    Backed by a bounded LRU (`_tokenize_cached`) so repeated tokenization of the
    same chunk/query text (dedup, keyword order, rule rerank all re-tokenize the
    same content several times per request) runs the regexes only once.
    """
    if not text:
        return []
    return list(_tokenize_cached(text))


def reset_tokenize_cache_for_tests() -> None:
    """Drop the process-global token cache (bounded; safe to clear any time)."""
    _tokenize_cached.cache_clear()


@dataclass
class _Doc:
    chunk_id: str
    unit_id: str
    chunk_index: int
    content: str
    version_id: str
    acl_department: str = ""
    acl_classification: str = ""
    heading_path: list[str] = field(default_factory=list)
    term_freq: Counter = field(default_factory=Counter)
    length: int = 0


class LexicalIndex:
    """Process-local inverted index. Multi-replica correctness is not a goal."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = float(k1)
        self._b = float(b)
        self._lock = threading.Lock()
        self._docs: dict[str, _Doc] = {}
        self._by_unit: dict[str, set[str]] = defaultdict(set)
        self._df: Counter = Counter()
        self._postings: dict[str, set[str]] = defaultdict(set)
        self._total_len = 0
        # Readiness gate: False until a full sync completes. Lets the retrieve
        # path skip a re-sync when the corpus is genuinely empty (previously it
        # re-synced on every request because doc_count() stayed 0).
        self._ready = False

    def is_ready(self) -> bool:
        """True once a full sync has completed (even for an empty corpus)."""
        return self._ready

    def mark_ready(self) -> None:
        self._ready = True

    def _doc_key(self, unit_id: str, chunk_id: str, version_id: str) -> str:
        return f"{unit_id}::{chunk_id}::{version_id}"

    def replace_unit(
        self,
        unit_id: str,
        version_id: str,
        docs: list[dict],
    ) -> int:
        """Atomically replace all docs for a unit (active version only)."""
        prepared: list[tuple[str, _Doc]] = []
        for d in docs:
            content = str(d.get("content") or "")
            chunk_id = str(d.get("id") or d.get("chunk_id") or d.get("chunk_index") or "")
            terms = tokenize(content)
            prepared.append(
                (
                    self._doc_key(unit_id, chunk_id, version_id),
                    _Doc(
                        chunk_id=chunk_id,
                        unit_id=unit_id,
                        chunk_index=int(d.get("chunk_index") or 0),
                        content=content,
                        version_id=version_id,
                        acl_department=str(d.get("acl_department") or ""),
                        acl_classification=str(d.get("acl_classification") or ""),
                        heading_path=list(d.get("heading_path") or []),
                        term_freq=Counter(terms),
                        length=max(1, len(terms)),
                    ),
                )
            )
        with self._lock:
            old_keys = self._by_unit.pop(unit_id, set())
            for key in old_keys:
                self._remove_doc_unlocked(key)
            for key, doc in prepared:
                self._docs[key] = doc
                self._by_unit[unit_id].add(key)
                self._total_len += doc.length
                for term in doc.term_freq:
                    self._df[term] += 1
                    self._postings[term].add(key)
            return len(prepared)

    def delete_unit(self, unit_id: str) -> int:
        with self._lock:
            keys = self._by_unit.pop(unit_id, set())
            for key in keys:
                self._remove_doc_unlocked(key)
            return len(keys)

    def _remove_doc_unlocked(self, key: str) -> None:
        doc = self._docs.pop(key, None)
        if not doc:
            return
        self._total_len = max(0, self._total_len - doc.length)
        for term in doc.term_freq:
            self._df[term] = max(0, self._df[term] - 1)
            if self._df[term] == 0:
                self._df.pop(term, None)
            postings = self._postings.get(term)
            if postings is not None:
                postings.discard(key)
                if not postings:
                    self._postings.pop(term, None)

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()
            self._by_unit.clear()
            self._df.clear()
            self._postings.clear()
            self._total_len = 0
        self._ready = False

    def doc_count(self) -> int:
        with self._lock:
            return len(self._docs)

    def search(
        self,
        query: str,
        top_k: int = 8,
        *,
        filter: SearchFilter | None = None,
        max_docs: int | None = None,
    ) -> list[dict]:
        terms = tokenize(query)
        if not terms:
            return []
        with self._lock:
            n_docs = max(1, len(self._docs))
            avgdl = (self._total_len / n_docs) if self._docs else 1.0
            df = Counter(self._df)
            # Inverted-index candidates only (avoid full scan + unit_id-order cap bias).
            cand_keys: set[str] = set()
            for term in set(terms):
                cand_keys |= self._postings.get(term, set())
            cap = int(max_docs or 0)
            if cap > 0 and len(cand_keys) > cap:
                # Safety valve only: stable key order, not unit_id corpus order.
                cand_keys = set(sorted(cand_keys)[:cap])
                logger.warning("lexical posting cap applied candidates>=%s", cap)
            docs = [self._docs[k] for k in cand_keys if k in self._docs]
            cand_n = len(docs)
        try:
            from core.circuit import get_metrics

            get_metrics().inc("lexical.posting_candidates", cand_n)
        except Exception:
            pass
        if not docs:
            return []

        k1 = self._k1
        b = self._b
        q_tf = Counter(terms)
        scored: list[dict] = []
        for doc in docs:
            row = {
                "id": doc.chunk_id,
                "unit_id": doc.unit_id,
                "chunk_index": doc.chunk_index,
                "content": doc.content,
                "version_id": doc.version_id,
                "acl_department": doc.acl_department,
                "acl_classification": doc.acl_classification,
                "heading_path": doc.heading_path,
            }
            if not row_passes_filter(row, filter):
                continue
            score = 0.0
            for term, qf in q_tf.items():
                tf = doc.term_freq.get(term, 0)
                if tf <= 0:
                    continue
                n_q = df.get(term, 0)
                idf = math.log(1 + (n_docs - n_q + 0.5) / (n_q + 0.5))
                denom = tf + k1 * (1 - b + b * doc.length / max(avgdl, 1e-9))
                score += idf * (tf * (k1 + 1) / denom) * qf
            if score <= 0:
                continue
            row["score"] = float(score)
            scored.append(row)
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[: max(1, top_k)]

    def sync_from_store(self, store: VectorStore, limit: int | None = None) -> int:
        """Rebuild from store active rows (memory/Milvus). Process-local only."""
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
            self._df.clear()
            self._postings.clear()
            self._total_len = 0
        count = 0
        for unit, docs in by_unit.items():
            count += self.replace_unit(unit, versions.get(unit, "legacy"), docs)
        self._ready = True
        logger.info("lexical index synced from store docs=%s units=%s", count, len(by_unit))
        return count


_default_index: LexicalIndex | None = None
_default_lock = threading.Lock()


def get_lexical_index() -> LexicalIndex:
    global _default_index
    with _default_lock:
        if _default_index is None:
            from core.config import get_settings

            cfg = get_settings()
            _default_index = LexicalIndex(
                k1=getattr(cfg, "lexical_k1", 1.5),
                b=getattr(cfg, "lexical_b", 0.75),
            )
        return _default_index


def reset_lexical_index_for_tests() -> None:
    """Drop the process-global lexical index so each test starts unready."""
    global _default_index
    with _default_lock:
        _default_index = None
