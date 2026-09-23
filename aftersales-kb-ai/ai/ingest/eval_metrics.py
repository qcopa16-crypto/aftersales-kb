"""Pure helpers for ingest eval observability (no I/O, no secrets)."""

from __future__ import annotations

import re
from typing import Any

# Permanent MinerU document faults — same family as mineru_client._DOC_FAULT_CODES.
PERMANENT_ERROR_CODES = frozenset(
    {
        "MINERU_EXTRACT_FAILED",
        "MINERU_EMPTY_MD",
        "MINERU_BAD_FILE",
        "MINERU_BAD_ZIP",
    }
)

STAGE_KEYS = ("extract_ms", "vlm_ms", "chunk_ms", "encode_ms", "publish_ms")

_SIZE_SPLIT = 5 * 1024 * 1024

_FAQ_RE = re.compile(r"(^|/)faq/", re.I)
_PRODUCT_RE = re.compile(r"(^|/)product/", re.I)
_POLICY_RE = re.compile(r"(^|/)(policy|sop|finance|sales)/", re.I)


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100). Empty → None."""
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    if p <= 0:
        return xs[0]
    if p >= 100:
        return xs[-1]
    # nearest-rank
    k = max(1, int((p / 100.0) * len(xs) + 0.999999999))  # ceil
    k = min(k, len(xs))
    return xs[k - 1]


def size_bucket(file_type: str, source_rel: str | None, file_bytes_len: int | None = None) -> str:
    """md / small_pdf / large_pdf / docx / other."""
    ft = (file_type or "").lower()
    if ft == "md":
        return "md"
    if ft == "txt":
        return "md"
    if ft == "docx":
        return "docx"
    if ft == "pdf":
        n = file_bytes_len
        if n is None and source_rel:
            # eval metrics may not have size; treat product manuals as large when path suggests
            n = None
        if n is None:
            return "large_pdf" if _PRODUCT_RE.search(source_rel or "") else "small_pdf"
        return "small_pdf" if int(n) < _SIZE_SPLIT else "large_pdf"
    return "other"


def default_chunk_size_for(
    file_type: str,
    rel: str | None = None,
    category_hint: str | None = None,
) -> int:
    """Per-type default chunk_size (S2.3). Explicit --chunk-size still overrides."""
    ft = (file_type or "").lower()
    path = rel or ""
    cat = (category_hint or "").strip().lower()
    if cat == "faq" or (_FAQ_RE.search(path) and ft != "pdf"):
        return 512
    if ft == "pdf":
        # Product manuals (or category=product) get the larger window from A/B.
        return 1000 if (cat == "product" or _PRODUCT_RE.search(path)) else 800
    if cat == "product":
        return 1000
    if cat in {"policy", "sop", "finance", "sales"} or _POLICY_RE.search(path):
        return 700
    if ft == "docx":
        return 700
    return 600


def quality_gate_warnings(
    *,
    chunk_count: int,
    avg_chunk_chars: float | None,
    empty_chunk_ratio: float | None,
    chunks_with_heading_ratio: float | None,
    image_failed: int = 0,
    table_header_missing: bool = False,
    suggest_review: bool = False,
    review_reasons: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Lightweight quality gates (S2.4) — advisory unless existing hard rules say otherwise."""
    warns: list[dict[str, Any]] = []
    avg = float(avg_chunk_chars or 0.0)
    empty = float(empty_chunk_ratio or 0.0)
    heading = float(chunks_with_heading_ratio if chunks_with_heading_ratio is not None else 1.0)

    if avg > 0 and avg < 120 and chunk_count > 30:
        warns.append(
            {
                "kind": "chunk_too_small",
                "message": f"avg_chunk_chars={avg:.1f}<120 and chunk_count={chunk_count}>30",
                "action": "increase_chunk_size",
                "threshold": "avg_chunk_chars<120 and chunk_count>30",
                "actual": f"avg={avg:.1f} n={chunk_count}",
            }
        )
    if empty > 0.02:
        warns.append(
            {
                "kind": "empty_ratio",
                "message": f"empty_chunk_ratio={empty:.4f}>0.02",
                "action": "review_splitter",
                "threshold": "empty_chunk_ratio>0.02",
                "actual": f"{empty:.4f}",
            }
        )
    if table_header_missing:
        warns.append(
            {
                "kind": "table_header_missing",
                "message": "table-like content missing header feature",
                "action": "quality_warn",
            }
        )
    if image_failed and image_failed > 0:
        warns.append(
            {
                "kind": "vlm_image_failed",
                "message": f"image_failed={image_failed}",
                "action": "retry_or_skip_images",
            }
        )
    if heading < 0.3 and chunk_count > 10:
        warns.append(
            {
                "kind": "low_heading_coverage",
                "message": f"chunks_with_heading_ratio={heading:.3f}<0.3",
                "action": "quality_warn",
            }
        )
    if suggest_review:
        for reason in review_reasons or []:
            warns.append(
                {
                    "kind": f"review_{reason}",
                    "message": f"suggest_review reason={reason}",
                    "action": "manual_qc",
                }
            )
    return warns


def classify_failure(error_code: str | None, detail: str = "") -> str:
    """permanent | transient | ok-path unused."""
    code = (error_code or "").strip()
    blob = f"{code} {detail}"
    if code in PERMANENT_ERROR_CODES or "MINERU_EXTRACT_FAILED" in blob:
        return "permanent"
    return "transient"


def build_ledgers(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Three fixed ledgers: permanent_fail / transient_fail / quality_warnings."""
    permanent: list[dict[str, Any]] = []
    transient: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []

    for r in results:
        m = r.get("metrics") or {}
        kind = r.get("kind")
        case_id = r.get("case_id")
        source_rel = m.get("source_rel") or r.get("source_rel")
        if kind == "positive" and not r.get("ok"):
            bucket = classify_failure(r.get("error_code"), str(r.get("detail") or ""))
            row = {
                "case_id": case_id,
                "source_rel": source_rel,
                "error_code": r.get("error_code"),
                "detail": (r.get("detail") or "")[:200],
            }
            if bucket == "permanent":
                row["action"] = "replace_source_or_exclude"
                permanent.append(row)
            else:
                row["attempts"] = m.get("attempts", 1)
                row["resumed"] = bool(m.get("resumed") or "resumed" in str(r.get("detail") or ""))
                transient.append(row)
        elif kind == "negative" and not r.get("ok"):
            transient.append(
                {
                    "case_id": case_id,
                    "source_rel": source_rel,
                    "error_code": r.get("error_code"),
                    "detail": (r.get("detail") or "")[:200],
                    "attempts": 1,
                }
            )

        for w in m.get("quality_warnings") or []:
            quality.append(
                {
                    "case_id": case_id,
                    "source_rel": source_rel,
                    "kind": w.get("kind"),
                    "message": w.get("message"),
                    "action": w.get("action"),
                    "threshold": w.get("threshold"),
                    "actual": w.get("actual"),
                }
            )

    return {
        "permanent_fail": permanent,
        "transient_fail": transient,
        "quality_warnings": quality,
    }


def latency_stats(
    rows: list[dict[str, Any]],
    metric_keys: tuple[str, ...] = ("parse_ms", "embed_ms", "elapsed_ms"),
) -> dict[str, Any]:
    """Percentiles overall and per size_bucket."""
    by_bucket: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        b = str((r.get("metrics") or {}).get("size_bucket") or "other")
        by_bucket.setdefault(b, []).append(r)

    def _one(sub: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in metric_keys:
            vals = [float((x.get("metrics") or {}).get(key) or 0.0) for x in sub]
            vals = [v for v in vals if v > 0]
            out[key] = {
                "n": len(vals),
                "p50": percentile(vals, 50),
                "p95": percentile(vals, 95),
                "p99": percentile(vals, 99),
                "avg": (sum(vals) / len(vals)) if vals else None,
            }
        return out

    positive = [r for r in rows if r.get("kind") == "positive"]
    pos_by_bucket: dict[str, list[dict[str, Any]]] = {}
    for r in positive:
        b = str((r.get("metrics") or {}).get("size_bucket") or "other")
        pos_by_bucket.setdefault(b, []).append(r)
    return {
        "overall": _one(positive),
        "by_bucket": {b: _one(sub) for b, sub in sorted(pos_by_bucket.items())},
    }


def stage_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean/sum for stage_timings + throughput."""
    positive = [r for r in rows if r.get("kind") == "positive"]
    stages: dict[str, Any] = {}
    for key in STAGE_KEYS:
        vals = []
        for r in positive:
            st = (r.get("metrics") or {}).get("stage_timings") or {}
            if key in st and st[key] is not None:
                vals.append(float(st[key]))
        stages[key] = {
            "n": len(vals),
            "sum": round(sum(vals), 1) if vals else 0.0,
            "avg": round(sum(vals) / len(vals), 1) if vals else None,
            "p95": percentile(vals, 95),
        }

    chunks_total = 0
    embed_ms_sum = 0.0
    elapsed_sum = 0.0
    for r in positive:
        m = r.get("metrics") or {}
        chunks_total += int(m.get("chunk_count") or 0)
        embed_ms_sum += float(m.get("embed_ms") or 0.0)
        elapsed_sum += float(m.get("elapsed_ms") or r.get("elapsed_ms") or 0.0)

    chunks_per_s = (chunks_total / (elapsed_sum / 1000.0)) if elapsed_sum > 0 else None
    embed_per_chunk = (embed_ms_sum / chunks_total) if chunks_total else None
    return {
        "stages": stages,
        "chunks_total": chunks_total,
        "chunks_per_s": round(chunks_per_s, 4) if chunks_per_s is not None else None,
        "embed_ms_per_chunk": round(embed_per_chunk, 2) if embed_per_chunk is not None else None,
    }


def compare_baseline(prev_summary: dict[str, Any], cur_summary: dict[str, Any]) -> dict[str, Any]:
    """Delta vs previous run: success rate, avg_chunk_chars, p95 parse/embed."""

    def _rate(s: dict[str, Any]) -> float | None:
        t = s.get("positive_total") or 0
        if not t:
            return None
        return round(100.0 * (s.get("positive_passed") or 0) / t, 2)

    out: dict[str, Any] = {
        "positive_rate_prev": _rate(prev_summary),
        "positive_rate_cur": _rate(cur_summary),
    }
    for key in ("avg_chunk_chars", "p95_parse_ms", "p95_embed_ms"):
        p, c = prev_summary.get(key), cur_summary.get(key)
        out[f"{key}_prev"] = p
        out[f"{key}_cur"] = c
        if p is not None and c is not None:
            out[f"{key}_delta"] = round(float(c) - float(p), 2)
    if out.get("positive_rate_prev") is not None and out.get("positive_rate_cur") is not None:
        out["positive_rate_delta"] = round(
            out["positive_rate_cur"] - out["positive_rate_prev"], 2
        )
    return out


def redact_secrets(text: str) -> str:
    """Strip accidental token-like substrings from report text."""
    if not text:
        return text
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._\-]{8,}", r"\1***", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+", r"\1***", text)
    return text
