"""Ingest content-coverage metrics: does the chunk set preserve the parsed content?

Pure, I/O-free helpers. Four sub-metrics answer "did ingestion lose content?":

1. ``char_coverage``   — visible text of the parse markdown vs. the union of chunk
   texts (normalized: whitespace / markdown syntax / table separators removed).
2. ``table``           — source table data rows vs. rows recoverable from chunks
   (row-granular chunking repeats the header on every piece, so header repeats are
   NOT counted as loss).
3. ``caption``         — VLM captions produced vs. captions actually present in chunks.
4. ``noise``           — share of chunks that are pure separators / heading leftovers
   (reuses :func:`ai.ingest.chunker._is_noise_piece`).

Why not raw character n-gram overlap? The chunker splits tables row-by-row **without
overlap** and repeats the header per piece; every chunk seam would then produce n-1
false "missing" n-grams per boundary. We therefore segment the markdown at natural
boundaries (table rows / sentences) and test each segment for contiguous presence in
some chunk. This is exact for the chunker's semantics and localizes real losses.

Normalization contract (fixed口径, must not be loosened):
  - drop all whitespace;
  - drop markdown image syntax, unwrap links, drop heading/blockquote markers, drop
    emphasis markers (``* _ ` ~``), drop HTML tags;
  - drop horizontal-rule lines and table separator lines (``|---|---|``);
  - a segment is *covered* iff its normalized text occurs contiguously in the
    normalized text of at least one chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ai.ingest.chunker import (
    _HEADING_RE,
    _SEPARATOR_RE,
    _SENT_SPLIT_RE,
    _is_noise_piece,
    _is_table_line,
)
from ai.media_refs import normalize_image_ref

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s*")
_BLOCKQUOTE_RE = re.compile(r"^>\s*")
# Leading list bullets / ordered markers are markdown syntax, not content.
_LIST_BULLET_RE = re.compile(r"^(?:[-+]|\d{1,3}[.)])\s+")
_EMPHASIS_RE = re.compile(r"[*_`~]+")
_WS_RE = re.compile(r"\s+")
_TABLE_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")

# Fixed normalization口径 tag, embedded in every report for auditability.
NORMALIZATION_TAG = "drop_ws+drop_md_syntax+drop_table_sep;segment=row|sentence|window40"

# Long unpunctuated runs get hard-split by ``_window_split`` (step = size - overlap).
# Segmenting at <= overlap (50) guarantees each window lands inside a single chunk
# because consecutive hard-split pieces overlap by exactly ``chunk_overlap``.
_SEGMENT_WINDOW = 40


def _is_table_sep_line(line: str) -> bool:
    s = line.strip()
    if not s or "-" not in s:
        return False
    if _TABLE_SEP_RE.match(s):
        return True
    # tolerate separator without outer pipes, e.g. "--- | ---"
    core = s.strip("|").strip()
    return bool(core) and set(core) <= set(": -|") and "-" in core


def clean_line(raw: str) -> str:
    """Strip markdown syntax from one line (no whitespace removal yet).

    Must run *before* sentence splitting: ``_SENT_SPLIT_RE`` treats ``!`` as a
    boundary and would otherwise fragment ``![](images/x.jpg)`` into pieces that
    no longer match the image regex.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if _SEPARATOR_RE.match(s) or _is_table_sep_line(s):
        return ""
    s = _BLOCKQUOTE_RE.sub("", s)
    s = _HEADING_PREFIX_RE.sub("", s)
    s = _LIST_BULLET_RE.sub("", s)
    s = _MD_IMG_RE.sub("", s)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _HTML_TAG_RE.sub("", s)
    s = _EMPHASIS_RE.sub("", s)
    return s.strip()


def normalize_visible(text: str) -> str:
    """Return the retrievable visible text: no whitespace, no markdown syntax."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        s = clean_line(raw)
        s = _WS_RE.sub("", s)
        if s:
            out.append(s)
    return "".join(out)


@dataclass
class Segment:
    text: str  # original (pre-normalization) text
    norm: str  # normalized text used for matching
    offset: int  # char offset of the source line in the markdown
    kind: str  # heading | table_row | body
    heading_path: list[str] = field(default_factory=list)


def _heading_stack_update(stack: list[str], line: str) -> list[str]:
    m = _HEADING_RE.match(line)
    if not m:
        return stack
    level = len(m.group(1))
    title = m.group(2).strip()
    while len(stack) >= level:
        stack.pop()
    stack.append(title)
    return stack


def iter_segments(markdown: str) -> list[Segment]:
    """Split markdown into coverage segments at table-row / sentence boundaries."""
    segments: list[Segment] = []
    stack: list[str] = []
    offset = 0
    for raw in (markdown or "").splitlines():
        line_start = offset
        offset += len(raw) + 1  # +1 for the stripped newline
        s = raw.strip()
        if not s:
            continue
        if _SEPARATOR_RE.match(s) or _is_table_sep_line(s):
            continue
        if _is_table_line(s):
            norm = normalize_visible(s)
            if norm:
                segments.append(
                    Segment(
                        text=s,
                        norm=norm,
                        offset=line_start,
                        kind="table_row",
                        heading_path=list(stack),
                    )
                )
            continue
        is_heading = bool(_HEADING_RE.match(s))
        stack = _heading_stack_update(stack, s)
        kind = "heading" if is_heading else "body"
        # Clean markdown syntax per line *before* splitting: keeps image/link
        # syntax intact so it can be stripped as a unit.
        cleaned = clean_line(s)
        parts = [cleaned] if is_heading else _SENT_SPLIT_RE.split(cleaned)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            norm = _WS_RE.sub("", part)
            if not norm:
                continue
            if kind == "table_row" or len(norm) <= _SEGMENT_WINDOW:
                segments.append(
                    Segment(
                        text=part, norm=norm, offset=line_start, kind=kind, heading_path=list(stack)
                    )
                )
            else:
                for i in range(0, len(norm), _SEGMENT_WINDOW):
                    win = norm[i : i + _SEGMENT_WINDOW]
                    if win:
                        segments.append(
                            Segment(
                                text=part,
                                norm=win,
                                offset=line_start,
                                kind=kind,
                                heading_path=list(stack),
                            )
                        )
    return segments


def _coerce_chunks(chunks: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, c in enumerate(chunks):
        if isinstance(c, Mapping):
            out.append(
                {
                    "chunk_index": c.get("chunk_index", i),
                    "content": c.get("content") or "",
                    "heading_path": list(c.get("heading_path") or []),
                }
            )
        else:  # object with attributes
            out.append(
                {
                    "chunk_index": getattr(c, "chunk_index", i),
                    "content": getattr(c, "content", "") or "",
                    "heading_path": list(getattr(c, "heading_path", []) or []),
                }
            )
    return out


# --------------------------------------------------------------------------- #
# 1. text coverage
# --------------------------------------------------------------------------- #
def char_coverage(
    markdown: str,
    chunks: Sequence[Any],
    *,
    max_fragments: int = 20,
    min_fragment_chars: int = 4,
) -> dict[str, Any]:
    chunk_dicts = _coerce_chunks(chunks)
    chunk_norms = [normalize_visible(c["content"]) for c in chunk_dicts]
    joined = "\x00".join(cn for cn in chunk_norms if cn)
    # heading title -> chunk indices (for locating the chunk that should hold a fragment)
    by_heading: dict[str, list[int]] = {}
    for c in chunk_dicts:
        hp = c["heading_path"]
        if hp:
            by_heading.setdefault(hp[-1], []).append(c["chunk_index"])

    segments = iter_segments(markdown)
    total_chars = 0
    covered_chars = 0
    missing: list[dict[str, Any]] = []
    missing_count = 0
    kind_total: dict[str, int] = {}
    kind_cov: dict[str, int] = {}
    for seg in segments:
        total_chars += len(seg.norm)
        kind_total[seg.kind] = kind_total.get(seg.kind, 0) + len(seg.norm)
        if seg.norm in joined:
            covered_chars += len(seg.norm)
            kind_cov[seg.kind] = kind_cov.get(seg.kind, 0) + len(seg.norm)
            continue
        missing_count += 1
        if len(seg.norm) < min_fragment_chars:
            continue
        if len(missing) >= max_fragments:
            continue
        head = seg.heading_path[-1] if seg.heading_path else None
        missing.append(
            {
                "kind": seg.kind,
                "offset": seg.offset,
                "nearest_heading": head,
                "expected_chunk_indices": (by_heading.get(head) or [])[:8] if head else [],
                "text": seg.norm[:160],
                "orig_snippet": seg.text[:200],
            }
        )

    coverage = (covered_chars / total_chars) if total_chars else 1.0
    by_kind = {
        k: {
            "chars_total": kind_total[k],
            "chars_covered": kind_cov.get(k, 0),
            "coverage": round(kind_cov.get(k, 0) / kind_total[k], 6) if kind_total[k] else 1.0,
        }
        for k in sorted(kind_total)
    }
    # "Substantive" coverage excludes pure-heading segments (structural labels the
    # chunker drops by design). Reported alongside, never used to replace the strict
    # headline number.
    body_total = sum(v["chars_total"] for k, v in by_kind.items() if k != "heading")
    body_cov = sum(v["chars_covered"] for k, v in by_kind.items() if k != "heading")
    return {
        "char_coverage": round(coverage, 6),
        "total_chars": total_chars,
        "covered_chars": covered_chars,
        "total_segments": len(segments),
        "missing_segments": missing_count,
        "missing_fragment_count": missing_count,
        "missing_fragments": missing,
        "by_kind": by_kind,
        "body_char_coverage": round(body_cov / body_total, 6) if body_total else 1.0,
    }


# --------------------------------------------------------------------------- #
# 2. table integrity
# --------------------------------------------------------------------------- #
def _parse_tables(markdown: str) -> list[dict[str, Any]]:
    """Return [{header, rows:[...]}] for each contiguous pipe-table block."""
    lines = (markdown or "").splitlines()
    tables: list[dict[str, Any]] = []
    i = 0
    n = len(lines)
    while i < n:
        if not _is_table_line(lines[i]):
            i += 1
            continue
        j = i
        while j < n and _is_table_line(lines[j]):
            j += 1
        block = [ln.strip() for ln in lines[i:j]]
        i = j
        if len(block) < 2:
            continue
        header = block[0]
        if _is_table_sep_line(block[1]):
            rows = block[2:]
        else:
            rows = block[1:]
        rows = [r for r in rows if not _is_table_sep_line(r)]
        tables.append({"header": header, "rows": rows})
    return tables


def table_integrity(markdown: str, chunks: Sequence[Any]) -> dict[str, Any]:
    chunk_dicts = _coerce_chunks(chunks)
    chunk_row_set: set[str] = set()
    for c in chunk_dicts:
        for line in c["content"].splitlines():
            s = line.strip()
            if _is_table_line(s) and not _is_table_sep_line(s):
                cn = normalize_visible(s)
                if cn:
                    chunk_row_set.add(cn)

    tables = _parse_tables(markdown)
    rows_total = 0
    recovered = 0
    missing_rows: list[str] = []
    for t in tables:
        for r in t["rows"]:
            rows_total += 1
            rn = normalize_visible(r)
            if rn and rn in chunk_row_set:
                recovered += 1
            else:
                missing_rows.append(r[:160])

    header_missing: list[int] = []
    for c in chunk_dicts:
        table_lines = [ln.strip() for ln in c["content"].splitlines() if _is_table_line(ln.strip())]
        if not table_lines:
            continue
        data = [ln for ln in table_lines if not _is_table_sep_line(ln)]
        has_sep = any(_is_table_sep_line(ln) for ln in table_lines)
        if data and not has_sep:
            header_missing.append(c["chunk_index"])

    coverage = (recovered / rows_total) if rows_total else 1.0
    return {
        "tables": len(tables),
        "rows_total": rows_total,
        "rows_recovered": recovered,
        "row_coverage": round(coverage, 6),
        "missing_rows": missing_rows[:20],
        "missing_row_count": len(missing_rows),
        "header_missing_chunks": header_missing[:20],
        "header_missing_count": len(header_missing),
    }


# --------------------------------------------------------------------------- #
# 3. caption coverage
# --------------------------------------------------------------------------- #
def caption_integrity(
    markdown: str,
    chunks: Sequence[Any],
    images: Sequence[Any] | None = None,
) -> dict[str, Any]:
    chunk_dicts = _coerce_chunks(chunks)
    joined = normalize_visible("\n".join(c["content"] for c in chunk_dicts))
    md_refs = {
        normalize_image_ref(p) for p in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown or "")
    }
    md_refs.discard("")
    md_bases = {r.rsplit("/", 1)[-1] for r in md_refs}
    md_bases.discard("")

    described: list[dict[str, Any]] = []
    for img in images or []:
        if isinstance(img, Mapping):
            status = img.get("status")
            caption = img.get("caption") or ""
            rel = img.get("relative_path")
        else:
            status = getattr(img, "status", None)
            caption = getattr(img, "caption", None) or ""
            rel = getattr(img, "relative_path", None)
        if status == "described" and caption.strip():
            described.append({"relative_path": rel, "caption": caption})

    total = len(described)
    injectable = 0
    present = 0
    not_injectable: list[dict[str, Any]] = []
    missing_in_chunks: list[dict[str, Any]] = []
    for img in described:
        rel = img["relative_path"]
        norm_rel = normalize_image_ref(rel or "")
        base_rel = norm_rel.rsplit("/", 1)[-1] if norm_rel else ""
        if not (norm_rel and (norm_rel in md_refs or base_rel in md_bases)):
            not_injectable.append(
                {"relative_path": img["relative_path"], "caption": img["caption"][:120]}
            )
            continue
        injectable += 1
        cn = normalize_visible(img["caption"])
        if cn and cn in joined:
            present += 1
        else:
            missing_in_chunks.append(
                {"relative_path": img["relative_path"], "caption": img["caption"][:120]}
            )

    coverage = (present / total) if total else 1.0
    injection_rate = (injectable / total) if total else 1.0
    injection_coverage = (present / injectable) if injectable else 1.0
    md_markers = (markdown or "").count("【图示】")
    chunk_markers = sum(c["content"].count("【图示】") for c in chunk_dicts)
    return {
        "captions_total": total,
        "captions_injectable": injectable,
        "captions_present": present,
        "caption_coverage": round(coverage, 6),
        "injection_rate": round(injection_rate, 6),
        "injection_coverage": round(injection_coverage, 6),
        "missing_captions": not_injectable[:20],
        "lost_in_chunks": missing_in_chunks[:20],
        "missing_caption_count": len(not_injectable) + len(missing_in_chunks),
        "caption_markers_in_markdown": md_markers,
        "caption_markers_in_chunks": chunk_markers,
    }


# --------------------------------------------------------------------------- #
# 4. noise ratio
# --------------------------------------------------------------------------- #
def noise_ratio(chunks: Sequence[Any]) -> dict[str, Any]:
    chunk_dicts = _coerce_chunks(chunks)
    indices = [c["chunk_index"] for c in chunk_dicts if _is_noise_piece(c["content"])]
    n = len(chunk_dicts)
    return {
        "noise_pieces": len(indices),
        "total_chunks": n,
        "noise_ratio": round(len(indices) / n, 6) if n else 0.0,
        "noise_chunk_indices": indices[:20],
    }


# --------------------------------------------------------------------------- #
# aggregator + warnings
# --------------------------------------------------------------------------- #
def compute_content_coverage(
    markdown: str,
    chunks: Sequence[Any],
    images: Sequence[Any] | None = None,
    *,
    max_fragments: int = 20,
    min_fragment_chars: int = 4,
) -> dict[str, Any]:
    """All four sub-metrics for one ingested unit."""
    text = char_coverage(
        markdown, chunks, max_fragments=max_fragments, min_fragment_chars=min_fragment_chars
    )
    table = table_integrity(markdown, chunks)
    caption = caption_integrity(markdown, chunks, images)
    noise = noise_ratio(chunks)
    return {
        "normalization": NORMALIZATION_TAG,
        "char_coverage": text["char_coverage"],
        "text": text,
        "table": table,
        "caption": caption,
        "noise": noise,
    }


def _as_metric(value: Any) -> float | None:
    """Coerce a coverage metric to ``float``, distinguishing "missing" from "zero".

    Returns ``None`` when the metric is **not usable** — either genuinely absent
    (``None``) or non-numeric (``""``, ``"abc"``, ...). Callers treat ``None`` as
    "metric not applicable → do not warn". A real ``0.0`` is returned unchanged so
    it is judged against the threshold like any other measured value.

    This is the fix for the ``float(x or 1.0)`` falsy trap, where a genuine
    ``0.0`` (e.g. every character lost) collapsed to the "perfect" default and
    silently failed *open*.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coverage_warnings(
    cov: Mapping[str, Any] | None,
    *,
    char_soft: float = 0.97,
    body_soft: float = 0.98,
    table_soft: float = 0.99,
    noise_soft: float = 0.0,
) -> list[dict[str, Any]]:
    """Soft (advisory) warnings derived from a content-coverage block.

    Metric semantics (falsy-safe, see :func:`_as_metric`): a metric that is
    **absent** (``None``) or **non-numeric** (``""``, ``"abc"``) is treated as
    "not applicable" and never warns. A metric that is a real number — **including
    ``0.0``** — is compared against its threshold, so a measured zero (e.g.
    ``char_coverage=0.0``, i.e. every segment lost) *does* warn. ``0.0`` must
    never be masked by a "perfect" default (the pre-fix ``float(x or 1.0)`` bug
    that failed the coverage gate *open*).

    Thresholds are set ~1pt below the 2026-10-08 full baseline
    (char_coverage min 0.978, body min 0.989, table_row 1.0, noise 0.0) so the
    frozen baseline stays clean while real regressions surface.

    The VLM-caption *injection* gap (caption produced for an image the markdown
    never references) is deliberately NOT a warning here: it is a pre-existing
    upstream issue reported in the coverage table (``injection_rate``), not a
    chunker regression. Only captions that were injected but then lost by the
    chunker are warned (``caption_chunk_loss``).
    """
    if not cov:
        return []
    warns: list[dict[str, Any]] = []

    text = cov.get("text") or {}
    cc = _as_metric(cov.get("char_coverage"))
    if cc is not None and cc < char_soft:
        frags = text.get("missing_fragments") or []
        sample = "; ".join(
            f"{f.get('nearest_heading') or '-'}@{f.get('offset')}:{str(f.get('text'))[:24]}"
            for f in frags[:3]
        )
        warns.append(
            {
                "kind": "content_coverage_low",
                "message": f"char_coverage={cc:.4f}<{char_soft} missing_segments={text.get('missing_segments')} [{sample}]",
                "action": "inspect_missing_fragments",
                "threshold": f"char_coverage<{char_soft}",
                "actual": f"{cc:.4f}",
            }
        )
    bc = _as_metric(text.get("body_char_coverage"))
    if bc is not None and bc < body_soft:
        frags = [f for f in (text.get("missing_fragments") or []) if f.get("kind") != "heading"]
        sample = "; ".join(
            f"{f.get('nearest_heading') or '-'}:{str(f.get('text'))[:24]}" for f in frags[:3]
        )
        warns.append(
            {
                "kind": "body_coverage_low",
                "message": f"body_char_coverage={bc:.4f}<{body_soft} [{sample}]",
                "action": "inspect_missing_fragments",
                "threshold": f"body_char_coverage<{body_soft}",
                "actual": f"{bc:.4f}",
            }
        )

    table = cov.get("table") or {}
    rc = _as_metric(table.get("row_coverage"))
    if rc is not None and rc < table_soft:
        warns.append(
            {
                "kind": "table_row_loss",
                "message": f"table_row_coverage={rc:.4f}<{table_soft} "
                f"missing_rows={table.get('missing_row_count')}",
                "action": "inspect_table_rows",
                "threshold": f"table_row_coverage<{table_soft}",
                "actual": f"{rc:.4f}",
            }
        )
    if table.get("header_missing_count"):
        warns.append(
            {
                "kind": "table_header_missing",
                "message": f"chunks_with_table_rows_but_no_header={table.get('header_missing_count')}",
                "action": "inspect_chunker",
                "threshold": "header_missing_count>0",
                "actual": str(table.get("header_missing_count")),
            }
        )

    caption = cov.get("caption") or {}
    inj_cov = _as_metric(caption.get("injection_coverage"))
    if int(caption.get("captions_injectable") or 0) > 0 and inj_cov is not None and inj_cov < 1.0:
        warns.append(
            {
                "kind": "caption_chunk_loss",
                "message": f"injected captions lost in chunks: injection_coverage={inj_cov:.4f}",
                "action": "inspect_chunker",
                "threshold": "injection_coverage<1.0",
                "actual": f"{inj_cov:.4f}",
            }
        )

    noise = cov.get("noise") or {}
    nr = _as_metric(noise.get("noise_ratio"))
    if nr is not None and nr > noise_soft:
        warns.append(
            {
                "kind": "noise_pieces",
                "message": f"noise_piece_ratio={nr:.4f}>{noise_soft} indices={noise.get('noise_chunk_indices')}",
                "action": "inspect_chunker",
                "threshold": f"noise_piece_ratio>{noise_soft}",
                "actual": f"{nr:.4f}",
            }
        )
    return warns


def aggregate_coverage(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Bucket/overall aggregates for a list of per-unit coverage blocks.

    Each row must be a mapping with at least ``char_coverage``; optional
    ``table``/``caption``/``noise`` sub-blocks and a ``bucket`` label.
    """
    rows = list(rows)
    overall = _agg_one(rows)
    by_bucket: dict[str, dict[str, Any]] = {}
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(str(r.get("bucket") or "other"), []).append(r)
    for b, sub in sorted(buckets.items()):
        by_bucket[b] = _agg_one(sub)
    return {"overall": overall, "by_bucket": by_bucket}


def _agg_one(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}

    def _mean(key: str) -> float | None:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 6) if vals else None

    def _min(key: str) -> float | None:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(min(vals), 6) if vals else None

    char_vals = [float(r.get("char_coverage") or 0.0) for r in rows]
    below = sum(1 for v in char_vals if v < 0.98)
    return {
        "n": n,
        "char_coverage_mean": _mean("char_coverage"),
        "char_coverage_min": _min("char_coverage"),
        "char_coverage_p05": _percentile(char_vals, 5),
        "units_below_0.98": below,
        "body_char_coverage_mean": _mean("body_char_coverage"),
        "body_char_coverage_min": _min("body_char_coverage"),
        "table_row_coverage_mean": _mean("table_row_coverage"),
        "table_row_coverage_min": _min("table_row_coverage"),
        "caption_coverage_mean": _mean("caption_coverage"),
        "caption_injection_rate_mean": _mean("caption_injection_rate"),
        "caption_total": sum(int(r.get("captions_total") or 0) for r in rows),
        "caption_injectable": sum(int(r.get("captions_injectable") or 0) for r in rows),
        "caption_present": sum(int(r.get("captions_present") or 0) for r in rows),
        "noise_ratio_max": max((float(r.get("noise_ratio") or 0.0) for r in rows), default=0.0),
        "chunks_total": sum(int(r.get("chunk_count") or 0) for r in rows),
        "chars_total": sum(int(r.get("total_chars") or 0) for r in rows),
    }


def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    k = max(1, min(len(xs), int((p / 100.0) * len(xs) + 0.999999999)))
    return round(xs[k - 1], 6)
