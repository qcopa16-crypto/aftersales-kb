"""Parse quality report for product QC gate (orchestration decides pass/fail)."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.config import Settings


# Stable enum for Java branching. Do not rename without contract bump.
REVIEW_REASONS = (
    "product_manual_heuristic",
    "image_count_high",
    "empty_chunks",
    "low_heading_coverage",
)


@dataclass
class QualityReport:
    chunk_count: int = 0
    empty_chunk_count: int = 0
    empty_chunk_ratio: float = 0.0
    avg_chunk_chars: float = 0.0
    max_chunk_chars: int = 0
    heading_count: int = 0
    chunks_with_heading_ratio: float = 0.0
    image_count: int = 0
    image_in_md_count: int = 0
    markdown_chars: int = 0
    page_count: int | None = None
    suggest_review: bool = False
    review_reasons: list[str] = field(default_factory=list)


def build_quality(
    *,
    markdown: str,
    chunks: list[tuple[str, list[str]]],  # (content, heading_path)
    image_count: int,
    image_in_md_count: int,
    page_count: int | None,
    file_type: str,
    cfg: Settings,
) -> QualityReport:
    md_chars = len(markdown or "")
    n = len(chunks)
    contents = [c for c, _ in chunks]
    empty = sum(1 for c in contents if not (c or "").strip())
    lengths = [len(c or "") for c in contents]
    with_heading = sum(1 for _, hp in chunks if hp)
    heading_titles = {hp[-1] for _, hp in chunks if hp}

    q = QualityReport(
        chunk_count=n,
        empty_chunk_count=empty,
        empty_chunk_ratio=(empty / n) if n else 0.0,
        avg_chunk_chars=(sum(lengths) / n) if n else 0.0,
        max_chunk_chars=max(lengths) if lengths else 0,
        heading_count=len(heading_titles),
        chunks_with_heading_ratio=(with_heading / n) if n else 0.0,
        image_count=image_count,
        image_in_md_count=image_in_md_count,
        markdown_chars=md_chars,
        page_count=page_count,
    )

    reasons: list[str] = []
    if file_type == "pdf":
        if (
            page_count is not None and page_count >= cfg.qc_pdf_page_review
        ) or md_chars >= cfg.qc_md_chars_review:
            reasons.append("product_manual_heuristic")
    elif md_chars >= cfg.qc_md_chars_review:
        reasons.append("product_manual_heuristic")

    if image_count >= cfg.qc_image_review:
        reasons.append("image_count_high")
    if n > 0 and q.empty_chunk_ratio > cfg.qc_empty_ratio_review:
        reasons.append("empty_chunks")
    if n > 10 and q.chunks_with_heading_ratio < cfg.qc_low_heading_ratio:
        reasons.append("low_heading_coverage")

    q.review_reasons = reasons
    q.suggest_review = bool(reasons)
    return q
