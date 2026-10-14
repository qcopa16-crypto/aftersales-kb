"""Pure helpers for session history: ACL filtering + prompt rendering.

Kept dependency-free and side-effect-free so they are trivially unit-testable and
reusable by both the generate (read/回灌) and retrieve-rewrite paths.

ACL rule (fail-closed, plan §2.3):
    keep turn  <=>  (authorized_unit_ids ∪ referenced_unit_ids) ⊆ acl_unit_ids
An empty set is a subset of anything, so FAQ / chat turns (no units) are kept.
A partially-authorized turn is dropped *whole* — never surgically trimmed
(the question text itself may name an inaccessible unit).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ai.session.store import SessionTurn

_CITATION_RE = re.compile(r"\[\d+\]")

# 单条 assistant 回答在历史块中的最大字符数（超出尾部截断）。
_ANSWER_MAX_CHARS = 400


def strip_citations(text: str) -> str:
    """Remove ``[n]`` citation markers so history cannot pollute this turn's [n]."""
    if not text:
        return ""
    return _CITATION_RE.sub("", text)


def filter_turns(
    turns: Iterable["SessionTurn"], acl_unit_ids: Iterable[str]
) -> list["SessionTurn"]:
    """Keep only turns fully covered by the caller-supplied accessible unit set.

    ``acl_unit_ids`` comes from Java (the source of truth); this function never
    invents an allow-set — it only intersects.
    """
    allowed = {str(u) for u in (acl_unit_ids or [])}
    kept: list["SessionTurn"] = []
    for turn in turns:
        used = {str(u) for u in (turn.authorized_unit_ids or [])}
        used |= {str(u) for u in (turn.referenced_unit_ids or [])}
        if used <= allowed:
            kept.append(turn)
    return kept


def render_history(
    turns: Iterable["SessionTurn"],
    *,
    max_turns: int = 4,
    max_chars: int = 2000,
) -> str:
    """Render the most recent turns as a plain-text block for the user prompt.

    Rendered as plain text (NOT ``role=assistant`` messages) so the model treats it
    as background for pronoun resolution, not as citable material. Oldest turns are
    dropped first when the char budget is exceeded.
    """
    items = list(turns)
    if max_turns and max_turns > 0:
        items = items[-max_turns:]
    blocks: list[str] = []
    for turn in items:
        q = (turn.question or "").strip()
        a = strip_citations((turn.answer or "").strip())
        if len(a) > _ANSWER_MAX_CHARS:
            a = a[:_ANSWER_MAX_CHARS] + "…"
        blocks.append(f"用户：{q}\n助手：{a}")
    # 从最旧轮开始丢，直到满足预算
    while blocks and len("\n\n".join(blocks)) > max_chars:
        blocks.pop(0)
    return "\n\n".join(blocks)
