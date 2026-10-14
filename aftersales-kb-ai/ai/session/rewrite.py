"""Retrieval-time query rewrite using session history (指代消解).

Given the current question and the *already ACL-filtered* recent turns, ask the LLM
to produce a self-contained question (resolving pronouns / ellipsis). On any failure
(timeout, LLM error, empty/oversized output) the ORIGINAL question is returned — the
rewrite is best-effort and must never break retrieval.

Callers MUST pass turns that have already been through
``ai.session.history.filter_turns`` — this module never touches ACLs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ai.session.history import render_history

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ai.session.store import SessionTurn

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM = (
    "你是检索查询改写助手。根据多轮对话历史，把用户的最新提问改写成一个**自包含**的检索查询："
    "补全指代（它/这个/那个/上面那个/那红色呢）与省略的主体。只输出改写后的查询本身，"
    "不要解释、不要引号、不要标点之外的额外文字。若最新提问本身已自包含，原样输出。"
)

_MAX_REWRITE_CHARS = 200


def rewrite_with_history(
    question: str,
    turns: Iterable["SessionTurn"],
    llm: Any,
    *,
    timeout: float = 3.0,
) -> str:
    """Return a self-contained query; falls back to ``question`` on any failure."""
    if not question or llm is None:
        return question
    items = list(turns)
    if not items:
        return question
    try:
        history = render_history(items, max_turns=4, max_chars=1200)
        if not history.strip():
            return question
        user = f"对话历史：\n{history}\n\n最新提问：{question}\n\n改写后的检索查询："
        out = llm.complete(
            _REWRITE_SYSTEM,
            user,
            max_tokens=120,
            timeout=timeout,
            disable_thinking=True,
        )
        out = (out or "").strip().strip('"').strip("“”").strip()
        # keep only the first line; reject oversized output
        if out:
            out = out.splitlines()[0].strip()
        if not out or len(out) > _MAX_REWRITE_CHARS:
            return question
        return out
    except Exception as exc:
        logger.warning("session rewrite failed, fallback to original question: %s", exc)
        return question
