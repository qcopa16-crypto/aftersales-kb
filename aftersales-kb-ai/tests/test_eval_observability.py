"""Unit tests for eval observability helpers (offline)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ai.ingest.eval_metrics import (  # noqa: E402
    build_ledgers,
    classify_failure,
    compare_baseline,
    default_chunk_size_for,
    latency_stats,
    percentile,
    quality_gate_warnings,
    redact_secrets,
    size_bucket,
    stage_stats,
)


def test_percentile_nearest_rank() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 95) == 5.0
    assert percentile([], 50) is None


def test_size_bucket() -> None:
    assert size_bucket("md", "docs/demo-knowledge/policy/x.md") == "md"
    assert size_bucket("pdf", "a.pdf", 1024) == "small_pdf"
    assert size_bucket("pdf", "a.pdf", 10 * 1024 * 1024) == "large_pdf"
    assert size_bucket("docx", "a.docx") == "docx"


def test_default_chunk_size_for() -> None:
    assert default_chunk_size_for("pdf", "docs/demo-knowledge/product/手册.pdf") == 1000
    assert default_chunk_size_for("pdf", "docs/demo-knowledge/other/手册.pdf") == 800
    assert default_chunk_size_for("md", "docs/demo-knowledge/policy/保修.md") == 700
    assert default_chunk_size_for("md", "docs/demo-knowledge/sop/工单.md") == 700
    assert default_chunk_size_for("md", "docs/demo-knowledge/faq/xx.md") == 512
    assert default_chunk_size_for("md", "docs/demo-knowledge/other.md") == 600
    assert default_chunk_size_for("md", "x.md", category_hint="product") == 1000


def test_quality_gates() -> None:
    warns = quality_gate_warnings(
        chunk_count=40,
        avg_chunk_chars=80,
        empty_chunk_ratio=0.05,
        chunks_with_heading_ratio=0.9,
        image_failed=1,
        table_header_missing=True,
        suggest_review=True,
        review_reasons=["empty_chunks"],
    )
    kinds = {w["kind"] for w in warns}
    assert "chunk_too_small" in kinds
    assert "empty_ratio" in kinds
    assert "table_header_missing" in kinds
    assert "vlm_image_failed" in kinds


def test_classify_and_ledgers() -> None:
    assert classify_failure("MINERU_EXTRACT_FAILED") == "permanent"
    assert classify_failure("MINERU_TIMEOUT") == "transient"
    ledgers = build_ledgers(
        [
            {
                "case_id": "a",
                "kind": "positive",
                "ok": False,
                "error_code": "MINERU_EXTRACT_FAILED",
                "detail": "MINERU_EXTRACT_FAILED",
                "metrics": {"source_rel": "docs/x.pdf", "quality_warnings": [{"kind": "empty_ratio"}]},
            },
            {
                "case_id": "b",
                "kind": "positive",
                "ok": False,
                "error_code": "MINERU_TIMEOUT",
                "detail": "timeout",
                "metrics": {"source_rel": "docs/y.pdf"},
            },
        ]
    )
    assert ledgers["permanent_fail"][0]["action"] == "replace_source_or_exclude"
    assert ledgers["transient_fail"][0]["case_id"] == "b"
    assert ledgers["quality_warnings"][0]["case_id"] == "a"


def test_latency_and_stage_stats() -> None:
    rows = [
        {
            "kind": "positive",
            "metrics": {
                "size_bucket": "md",
                "parse_ms": 10,
                "embed_ms": 100,
                "elapsed_ms": 110,
                "chunk_count": 2,
                "stage_timings": {"encode_ms": 80, "publish_ms": 20},
            },
        },
        {
            "kind": "positive",
            "metrics": {
                "size_bucket": "large_pdf",
                "parse_ms": 1000,
                "embed_ms": 500,
                "elapsed_ms": 1500,
                "chunk_count": 10,
                "stage_timings": {"extract_ms": 900, "chunk_ms": 10},
            },
        },
    ]
    lat = latency_stats(rows)
    assert "md" in lat["by_bucket"]
    assert lat["overall"]["parse_ms"]["p95"] is not None
    st = stage_stats(rows)
    assert st["stages"]["encode_ms"]["n"] == 1
    assert st["chunks_total"] == 12


def test_baseline_and_redact() -> None:
    delta = compare_baseline(
        {"positive_passed": 9, "positive_total": 10},
        {"positive_passed": 10, "positive_total": 10},
    )
    assert delta["positive_rate_delta"] == 10.0
    s = redact_secrets("INTERNAL_TOKEN=abc123secret Bearer eyJhbGciOi")
    assert "abc123secret" not in s
    assert "***" in s
