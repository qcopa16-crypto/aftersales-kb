"""Ingest review gate: three-tier verdict (rules → LLM → VLM).

AI only *produces* a verdict/confidence/reasons; the Java orchestration decides
whether to auto-pass or route to a human (see docs/ingest-quality-gate.md and the
"Python 不实现质检确认 API" boundary in docs/ai-service-api.md).

This module must NOT import ``app.*`` (architecture boundary gate,
``tests/test_architecture_boundaries.py``) — it only depends on ``ai/*`` /
``core/*`` / ``schemas/*``.

Tier semantics (see ``ReviewResponse.tiers``):

* ``rules``: ``"pass"`` | ``"review"`` — deterministic quality signals.
* ``llm``:   ``"pass"`` | ``"review"`` | ``"skipped"`` | ``"error"``.
* ``vlm``:   ``"pass"`` | ``"review"`` | ``"skipped"`` | ``"unavailable"`` | ``"error"``.

Any tier that is not ``pass``/``skipped`` fails the whole verdict closed
(``verdict="review"``). A tier-level failure never raises an HTTP error — it is
recorded in ``tiers`` so a VLM outage cannot discard a usable LLM verdict.

HTTP contract (阶段2 Java depends on this): **tier-level failures return HTTP 200
with details in ``tiers``**; only *request-level* problems use 400
(``REVIEW_BAD_REQUEST``) / 503 (``REVIEW_DISABLED``) / 504 (``REVIEW_TIMEOUT``).
A 502 ``REVIEW_LLM_FAILED`` / ``REVIEW_VLM_FAILED`` is intentionally **not
implemented**. The endpoint's 504 is a post-hoc budget report; the hard wall-clock
timeout is owned by the Java side (``ai.review-timeout-ms``).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import re
import time
from typing import Any

from ai.generate.vlm import guess_mime
from ai.ingest.coverage import compute_content_coverage, coverage_warnings
from ai.ingest.fingerprint import content_fingerprint
from ai.ingest.quality import build_quality
from ai.ingest.review_image_cache import load_review_image
from core.config import Settings
from schemas.ingest import ReviewRequest, ReviewResponse, VlmFinding

logger = logging.getLogger(__name__)

# Stable enum for Java branching. Do not rename without contract bump.
REVIEW_VERDICTS = ("pass", "review")

# 独立常量：严禁改 ai/generate/llm.py::_SYSTEM_PROMPT（有 sha256 快照断言）。
_REVIEW_LLM_SYSTEM = (
    "你是售后知识库的入库质检员。判断这份文档切片是否内容完整、结构清晰、可用于检索问答。\n"
    '只输出 JSON：{"verdict":"pass|review","confidence":0.0,"reasons":["..."]}。\n'
    "confidence 为 0~1；有缺章、乱码、空内容、结构错乱时给 review。"
)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")

# reason tokens that are machine-readable (prefix:value)
_RULE_EMPTY = "rules:empty_chunks"
_RULE_HEADING = "rules:low_heading_coverage"
_RULE_COVERAGE = "rules:coverage_warnings"
_RULE_NO_CHUNKS = "rules:no_chunks"
_RULE_EMPTY_DOC = "rules:empty_document"
_RULE_CLEAN = "rules:clean"

# Hard vetoes: structural defects the LLM/VLM may NOT override.
_RULE_VETOES = frozenset({_RULE_NO_CHUNKS, _RULE_EMPTY_DOC})

# Maps a ``coverage_warnings`` kind to the fine-grained ``coverage:*`` trigger
# that already represents the same root cause. Used to dedup the rules tier:
# ``rules:coverage_warnings`` is dropped when every warning is already covered by
# a precise trigger (it is only a coarse fallback for warnings without one, e.g.
# ``caption_chunk_loss``).
_WARN_KIND_TO_TRIGGER = {
    "content_coverage_low": "coverage:char_coverage_low",
    "body_coverage_low": "coverage:body_coverage_low",
    "table_row_loss": "coverage:table_integrity",
    "table_header_missing": "coverage:table_integrity",
    "noise_pieces": "coverage:noise_pieces",
}


def parse_verdict_json(raw: str) -> tuple[str, float, list[str]] | None:
    """Tolerant JSON extraction for an LLM verdict.

    Reuses the ``answer_metrics._parse_judge_score`` discipline: grab the first
    ``{...}`` block and ``json.loads`` it. Returns ``None`` on any failure
    (no JSON, bad JSON, or an unknown ``verdict``) so the caller can fail closed.
    """
    if not raw:
        return None
    m = _JSON_BLOCK_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in REVIEW_VERDICTS:
        return None
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = round(min(1.0, max(0.0, confidence)), 4)
    raw_reasons = obj.get("reasons")
    reasons = [str(r) for r in raw_reasons] if isinstance(raw_reasons, list) else []
    return verdict, confidence, reasons


def build_llm_verdict(
    req: ReviewRequest,
    quality: dict[str, Any],
    coverage: dict[str, Any],
    warnings: list[dict[str, Any]],
    *,
    llm: Any,
    cfg: Settings,
) -> tuple[str, float, list[str]] | None:
    """Call the LLM and parse its JSON verdict.

    Raises whatever ``llm.complete`` raises (e.g. ``LlmError``); returns ``None``
    when the response cannot be parsed into a valid verdict. The caller maps both
    cases to ``tiers.llm="error"`` / ``"review"`` (fail-closed).
    """
    user = _build_llm_user_prompt(req, quality, coverage, warnings, cfg)
    raw = llm.complete(
        _REVIEW_LLM_SYSTEM,
        user,
        max_tokens=cfg.review_llm_max_tokens,
        timeout=cfg.review_llm_timeout_sec,
        disable_thinking=True,
    )
    return parse_verdict_json(raw)


def _build_llm_user_prompt(
    req: ReviewRequest,
    quality: dict[str, Any],
    coverage: dict[str, Any],
    warnings: list[dict[str, Any]],
    cfg: Settings,
) -> str:
    md = req.markdown or ""
    md = md[: max(0, cfg.review_markdown_max_chars)]
    max_chunks = max(0, cfg.review_chunks_max)
    chunk_lines: list[str] = []
    for i, c in enumerate(req.chunks[:max_chunks]):
        content = str(c.get("content") or "")
        heading = c.get("heading_path") or []
        head = " > ".join(str(h) for h in heading) if isinstance(heading, list) else str(heading)
        chunk_lines.append(f"[{i}] heading={head or '-'} content={content[:400]}")
    warn_kinds = ",".join(str(w.get("kind")) for w in warnings) or "-"
    return (
        f"category={req.category or 'unknown'}\n"
        f"quality: chunk_count={quality.get('chunk_count', 0)} "
        f"empty_chunk_ratio={quality.get('empty_chunk_ratio', 0.0)} "
        f"chunks_with_heading_ratio={quality.get('chunks_with_heading_ratio', 0.0)} "
        f"image_count={quality.get('image_count', 0)} "
        f"markdown_chars={quality.get('markdown_chars', 0)}\n"
        f"coverage_warnings: {warn_kinds}\n"
        f"char_coverage={coverage.get('char_coverage')}\n"
        f"markdown:\n{md}\n\n"
        f"chunks:\n" + "\n".join(chunk_lines)
    )


def resolve_review_images(req: ReviewRequest, cfg: Settings) -> list[tuple[str, bytes]]:
    """Resolve image bytes for the VLM spot-check.

    Priority per image: ``images[].data_b64`` (caller-supplied) → AI-side disk
    cache keyed by ``content_hash`` (written during parse, see
    ``ai.ingest.review_image_cache``) → unavailable. When nothing resolves the
    caller marks ``tiers.vlm="unavailable"`` (fail-closed); never fabricated.
    """
    max_images = max(0, cfg.review_vlm_max_images)
    # Cache key == ParseResponse.content_hash == content_fingerprint(markdown).
    # Fall back to recomputing it when the caller omitted content_hash.
    chash = req.content_hash or content_fingerprint(req.markdown or "")
    out: list[tuple[str, bytes]] = []
    for im in req.images:
        if len(out) >= max_images:
            break
        blob = b""
        if im.data_b64:
            try:
                blob = base64.b64decode(im.data_b64, validate=False)
            except (ValueError, TypeError):
                blob = b""
        if not blob and chash:
            cached = load_review_image(chash, im.relative_path, cfg)
            if cached:
                blob = cached
        if blob:
            out.append((im.relative_path, blob))
    return out


def vlm_spot_check(
    images: list[tuple[str, bytes]],
    *,
    vlm: Any,
    cfg: Settings,
) -> tuple[str, list[VlmFinding]]:
    """Assess up to ``review_vlm_max_images`` images.

    Returns ``(tier, findings)`` with tier in
    ``{"pass", "review", "unavailable", "error"}``:

    * empty ``images`` → ``"unavailable"`` (no bytes to check).
    * any image ``usable=false`` → ``"review"``.
    * any call raising → ``"error"`` (fail-closed).
    * cumulative wall-clock over ``review_vlm_timeout_sec`` → ``"error"`` (aborts
      early instead of running all images).
    * all usable → ``"pass"``.
    """
    if not images:
        return "unavailable", []
    budget = float(getattr(cfg, "review_vlm_timeout_sec", 0.0) or 0.0)
    t0 = time.monotonic()
    findings: list[VlmFinding] = []
    for rel, blob in images:
        elapsed = time.monotonic() - t0
        if budget > 0 and elapsed > budget:
            logger.warning(
                "review VLM budget %.1fs exhausted after %d image(s)", budget, len(findings)
            )
            return "error", findings
        mime = guess_mime(rel)
        call_timeout = (budget - elapsed) if budget > 0 else None
        try:
            usable, reason = vlm.assess_image(blob, mime=mime, timeout=call_timeout)
        except Exception:  # noqa: BLE001 - any VLM failure is fail-closed
            logger.warning("review VLM assess failed path=%s", rel, exc_info=True)
            return "error", findings
        findings.append(VlmFinding(relative_path=rel, usable=bool(usable), reason=reason or ""))
    if any(not f.usable for f in findings):
        return "review", findings
    return "pass", findings


def _quality_dict(req: ReviewRequest, cfg: Settings) -> dict[str, Any]:
    """Return the quality summary, recomputing via ``build_quality`` when absent."""
    if req.quality:
        return dict(req.quality)
    chunk_tuples: list[tuple[str, list[str]]] = [
        (str(c.get("content") or ""), list(c.get("heading_path") or [])) for c in req.chunks
    ]
    image_in_md = len(_MD_IMAGE_RE.findall(req.markdown or ""))
    report = build_quality(
        markdown=req.markdown or "",
        chunks=chunk_tuples,
        image_count=len(req.images),
        image_in_md_count=image_in_md,
        page_count=None,
        file_type="",
        cfg=cfg,
    )
    return dataclasses.asdict(report)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coverage_triggers(coverage: dict[str, Any]) -> list[str]:
    """Explicit coverage checks that distinguish ``None`` from ``0.0``.

    ``coverage_warnings`` masks falsy values with ``or 1.0``, so a real
    ``char_coverage=0.0`` (nothing covered) would fail *open*. These thresholds
    mirror its soft thresholds but treat ``0.0`` as a trigger and only skip
    genuinely-missing (``None``) metrics.
    """
    if not coverage:
        return []
    out: list[str] = []
    cc = _as_float(coverage.get("char_coverage"))
    if cc is not None and cc < 0.97:
        out.append("coverage:char_coverage_low")
    text = coverage.get("text") or {}
    bc = _as_float(text.get("body_char_coverage"))
    if bc is not None and bc < 0.98:
        out.append("coverage:body_coverage_low")
    table = coverage.get("table") or {}
    rc = _as_float(table.get("row_coverage"))
    hdr = table.get("header_missing_count")
    header_missing = False
    try:
        header_missing = int(hdr or 0) > 0
    except (TypeError, ValueError):
        header_missing = False
    if (rc is not None and rc < 0.99) or header_missing:
        out.append("coverage:table_integrity")
    noise = coverage.get("noise") or {}
    nr = _as_float(noise.get("noise_ratio"))
    if nr is not None and nr > 0.0:
        out.append("coverage:noise_pieces")
    return out


def _rules_verdict(
    quality: dict[str, Any],
    coverage: dict[str, Any],
    warnings: list[dict[str, Any]],
    *,
    markdown: str,
    chunks: list[Any],
    cfg: Settings,
) -> tuple[str, list[str]]:
    """Deterministic rules tier. ``review`` iff any content-quality trigger fires."""
    reasons: list[str] = []
    empty_ratio = _as_float(quality.get("empty_chunk_ratio")) or 0.0
    heading_ratio = _as_float(quality.get("chunks_with_heading_ratio")) or 0.0
    chunk_count = int(_as_float(quality.get("chunk_count")) or 0)

    if empty_ratio > cfg.qc_empty_ratio_review:
        reasons.append(_RULE_EMPTY)
    if heading_ratio < cfg.qc_low_heading_ratio and chunk_count > 10:
        reasons.append(_RULE_HEADING)
    triggers = _coverage_triggers(coverage)
    # Dedup: keep the precise ``coverage:*`` reasons; fall back to the coarse
    # ``rules:coverage_warnings`` only when at least one warning has no
    # fine-grained counterpart (e.g. ``caption_chunk_loss``).
    if any(_WARN_KIND_TO_TRIGGER.get(str(w.get("kind"))) not in triggers for w in warnings):
        reasons.append(_RULE_COVERAGE)
    reasons.extend(triggers)
    # Structural fail-open fixes (hard vetoes):
    #   * blank markdown → nothing verifiable, regardless of chunks;
    #   * non-blank markdown with no chunks → nothing retrievable.
    if not (markdown or "").strip():
        reasons.append(_RULE_EMPTY_DOC)
    elif not chunks:
        reasons.append(_RULE_NO_CHUNKS)
    if reasons:
        return "review", reasons
    return "pass", [_RULE_CLEAN]


def _review_coverage(req: ReviewRequest) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    coverage = compute_content_coverage(req.markdown or "", req.chunks or [], req.images or [])
    return coverage, coverage_warnings(coverage)


def build_review(
    req: ReviewRequest,
    *,
    llm: Any,
    vlm: Any,
    cfg: Settings,
) -> ReviewResponse:
    """Orchestrate the three tiers. Never raises on an internal error — an
    unexpected failure yields ``verdict="review"`` with the failing tier marked
    ``error`` (fail-closed)."""
    llm_ms = 0.0
    vlm_ms = 0.0
    try:
        quality = _quality_dict(req, cfg)
        coverage, warnings = _review_coverage(req)

        rules_tier, reasons = _rules_verdict(
            quality,
            coverage,
            warnings,
            markdown=req.markdown or "",
            chunks=req.chunks or [],
            cfg=cfg,
        )

        llm_tier = "skipped"
        confidence = 1.0
        llm_model_used = False
        if rules_tier != "pass" and req.enable_llm and cfg.review_llm_enabled:
            t0 = time.monotonic()
            try:
                parsed = build_llm_verdict(req, quality, coverage, warnings, llm=llm, cfg=cfg)
            except Exception:  # noqa: BLE001 - LLM outage is fail-closed
                logger.warning("review LLM call failed unit=%s", req.unit_id, exc_info=True)
                parsed = None
            finally:
                llm_ms = round((time.monotonic() - t0) * 1000, 3)
            llm_model_used = True
            if parsed is None:
                llm_tier = "error"
                confidence = 0.0
                reasons.append("llm:error")
            else:
                llm_verdict, llm_conf, llm_reasons = parsed
                llm_tier = llm_verdict
                confidence = llm_conf
                reasons.append(f"llm:{llm_verdict}")
                for r in llm_reasons[:5]:
                    reasons.append(f"llm:{r}")

        image_count = 0
        try:
            image_count = int(quality.get("image_count") or 0)
        except (TypeError, ValueError):
            image_count = 0

        vlm_tier = "skipped"
        vlm_findings: list[VlmFinding] = []
        vlm_model_used = False
        if req.enable_vlm and cfg.review_vlm_enabled and image_count >= cfg.qc_image_review:
            images = resolve_review_images(req, cfg)
            t0 = time.monotonic()
            vlm_tier, vlm_findings = vlm_spot_check(images, vlm=vlm, cfg=cfg)
            vlm_ms = round((time.monotonic() - t0) * 1000, 3)
            vlm_model_used = True
            if vlm_tier == "review":
                reasons.append("vlm:unusable_image")
            elif vlm_tier == "unavailable":
                reasons.append("vlm:unavailable")
            elif vlm_tier == "error":
                reasons.append("vlm:error")
            else:
                reasons.append("vlm:pass")

        tiers = {"rules": rules_tier, "llm": llm_tier, "vlm": vlm_tier}
        # Final verdict (plan §2.1): the rules tier is a *trigger*; once it fires
        # the LLM becomes the content-decider. So:
        #   * rules == "pass"            → no AI needed, auto-pass unless VLM fails
        #   * rules == "review"          → require LLM == "pass" (else fail-closed)
        #   * VLM (when triggered) must be "pass" or "skipped"
        # EXCEPT `rules:no_chunks` / `rules:empty_document`: *structural* defects
        # (nothing retrievable) are hard vetoes neither the LLM nor the VLM may
        # override.
        veto = any(r in _RULE_VETOES for r in reasons)
        rules_ok = rules_tier == "pass" or llm_tier == "pass"
        vlm_ok = vlm_tier in ("pass", "skipped")
        verdict = "pass" if (not veto and rules_ok and vlm_ok) else "review"

        model = ""
        if llm_model_used:
            model = str(getattr(cfg, "llm_model", "") or "")
        elif vlm_model_used:
            model = str(getattr(cfg, "vlm_model", "") or "")

        return ReviewResponse(
            verdict=verdict,
            confidence=confidence if verdict == "pass" else min(confidence, 1.0),
            reasons=reasons,
            tiers=tiers,
            coverage=coverage,
            vlm_findings=vlm_findings,
            model=model,
            stage_timings={"llm_ms": llm_ms, "vlm_ms": vlm_ms},
            ingest_id=req.ingest_id,
        )
    except Exception:  # noqa: BLE001 - never 500 the ingest worker
        logger.exception("review build failed unit=%s", getattr(req, "unit_id", "?"))
        return ReviewResponse(
            verdict="review",
            confidence=0.0,
            reasons=["internal:error"],
            tiers={"rules": "review", "llm": "error", "vlm": "skipped"},
            coverage={},
            vlm_findings=[],
            model="",
            stage_timings={"llm_ms": llm_ms, "vlm_ms": vlm_ms},
            ingest_id=getattr(req, "ingest_id", None),
        )
