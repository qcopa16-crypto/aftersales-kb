"""Prompt-side context packing for /internal/generate.

The raw retrieve pipeline hands back chunks in retrieval order. Dumping them
verbatim into the prompt (a) lets near-duplicate windows burn the budget and
(b) gives the model no stable handle to cite. This module:

  1. orders chunks by relevance score (stable),
  2. greedily drops near-duplicates (Jaccard over character shingles),
  3. truncates each chunk and stops at a character budget,
  4. assigns 1-based citation indices so the answer can emit `[n]`.

Pure and I/O-free, so it is unit-testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

_DEFAULT_MAX_CHARS = 6000
_DEFAULT_PER_CHUNK = 1500
_DEFAULT_JACCARD = 0.85


@dataclass
class PackedContext:
    unit_id: str
    title: str
    content: str
    score: float
    index: int  # 1-based citation index


def _shingles(text: str, n: int = 2) -> set[str]:
    """Character n-grams over whitespace-stripped lowercase text (CJK-friendly)."""
    s = "".join((text or "").lower().split())
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _title_of(chunk: dict[str, Any]) -> str:
    title = str(chunk.get("title") or "").strip()
    if title:
        return title
    path = chunk.get("heading_path") or []
    if path:
        return str(path[-1]).strip()
    return ""


def _score_of(chunk: dict[str, Any]) -> float:
    for key in ("score_fused", "score", "score_dense"):
        val = chunk.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def pack_contexts(
    chunks: Iterable[dict[str, Any]],
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    per_chunk_max: int = _DEFAULT_PER_CHUNK,
    jaccard_threshold: float = _DEFAULT_JACCARD,
    max_items: int = 0,
) -> list[PackedContext]:
    """Dedup + budget + number contexts. Never raises on malformed input."""
    items: list[tuple[float, int, dict[str, Any]]] = []
    for order, chunk in enumerate(chunks or []):
        if not isinstance(chunk, dict):
            continue
        if not str(chunk.get("content") or "").strip():
            continue
        items.append((_score_of(chunk), order, chunk))

    # Relevance first; original order breaks ties so the result is deterministic.
    items.sort(key=lambda t: (-t[0], t[1]))

    kept: list[PackedContext] = []
    kept_shingles: list[set[str]] = []
    total = 0
    budget = max(1, int(max_chars))
    per_cap = max(1, int(per_chunk_max))
    thr = float(jaccard_threshold)
    limit = int(max_items)

    for score, _order, chunk in items:
        content = str(chunk.get("content") or "").strip()[:per_cap]
        sh = _shingles(content)
        if any(jaccard(sh, prev) >= thr for prev in kept_shingles):
            continue
        if kept and total + len(content) > budget:
            continue
        kept.append(
            PackedContext(
                unit_id=str(chunk.get("unit_id") or ""),
                title=_title_of(chunk),
                content=content,
                score=score,
                index=len(kept) + 1,
            )
        )
        kept_shingles.append(sh)
        total += len(content)
        if limit and len(kept) >= limit:
            break

    return kept
