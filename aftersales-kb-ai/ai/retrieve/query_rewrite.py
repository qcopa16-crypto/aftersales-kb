"""Optional LLM query rewrite for the retrieve pipeline.

Rewrites the user question into a more retrieval-friendly query (synonym/jargon
normalization). Always falls back to the original question on any failure, so
the default path is unaffected. Disabled unless `retrieve_query_rewrite_enabled`
is true and an LLM is configured.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_REWRITE_PROMPT = (
    "你是售后知识库检索助手。请把用户问题改写为一条更利于检索的查询："
    "**保留原问题的全部关键信息（对象、动作、条件），不要省略或抽象**，"
    "在此基础上补充同义词、规范术语或错误码（如 E-05、Reset、搓纸轮）。"
    "不要回答问题，只输出改写后的查询，不超过 60 字。"
)


class QueryRewriter(ABC):
    @abstractmethod
    def rewrite(self, question: str) -> str | None: ...


class LlmQueryRewriter(QueryRewriter):
    def __init__(self, llm: Any, timeout: float = 3.0) -> None:
        self._llm = llm
        self._timeout = timeout

    def rewrite(self, question: str) -> str | None:
        try:
            out = self._llm.complete(
                _REWRITE_PROMPT,
                question,
                max_tokens=128,
                timeout=self._timeout,
                # Short-output helper: a reasoning model would otherwise burn the
                # whole budget on hidden reasoning and return empty content.
                disable_thinking=True,
            )
            out = (out or "").strip()
            return out or None
        except Exception:
            logger.warning("query rewrite failed; fallback to original", exc_info=True)
            return None


def create_query_rewriter(cfg: Any, llm: Any) -> QueryRewriter | None:
    if not getattr(cfg, "retrieve_query_rewrite_enabled", False):
        return None
    if llm is None or not getattr(llm, "available", False):
        return None
    return LlmQueryRewriter(
        llm, timeout=float(getattr(cfg, "retrieve_query_rewrite_timeout_sec", 3.0) or 3.0)
    )


_rewriter: QueryRewriter | None = None


def get_query_rewriter(cfg: Any | None = None, llm: Any | None = None) -> QueryRewriter | None:
    global _rewriter
    if _rewriter is None:
        _rewriter = create_query_rewriter(cfg, llm)
    return _rewriter


# Small process-local LRU for rewritten queries (avoid repeat LLM calls).
_cache: OrderedDict[str, str] = OrderedDict()
_cache_lock = threading.Lock()


def rewrite_question_cached(
    question: str,
    *,
    rewriter: QueryRewriter | None,
    max_size: int = 0,
) -> str:
    """Return rewritten question, falling back to the original."""
    if rewriter is None:
        return question
    if max_size > 0:
        key = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key]
    out = rewriter.rewrite(question)
    if not out:
        return question
    if max_size > 0:
        with _cache_lock:
            _cache[key] = out
            _cache.move_to_end(key)
            while len(_cache) > max_size:
                _cache.popitem(last=False)
    return out
