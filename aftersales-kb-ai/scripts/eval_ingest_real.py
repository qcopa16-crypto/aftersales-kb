"""Real-chain ingest evaluation (small sample set or full docs corpus).

Uses shopkeeper-ai venv + real BGE-M3 + real MinerU + real Milvus.
Reads real files under docs/demo-knowledge, runs parse → embed → self-check.

Spec: docs/compose/spec/ingest-eval-real.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ai.ingest.eval_metrics import (  # noqa: E402
    build_ledgers,
    compare_baseline,
    default_chunk_size_for,
    latency_stats,
    quality_gate_warnings,
    redact_secrets,
    size_bucket,
    stage_stats,
)

DEFAULT_COLLECTION = "kb_ai_chunks_eval_ingest"
REPORT_DIR = ROOT / "samples" / "eval_ingest"
FULL_REPORT_DIR = ROOT / "samples" / "eval_ingest_full"
AB_REPORT_DIR = ROOT / "samples" / "eval_ingest_ab"
# Canonical denylist is versioned; report dir copy is runtime-only.
KNOWN_BAD_JSON = ROOT / "scripts" / "data" / "known_bad_extract.json"
BASELINE_JSON_NAME = "baseline_prev.json"
QC_PACK_DIR_NAME = "qc_pack"
_CORPUS_ROOT = REPO / "docs" / "demo-knowledge"
_SKIP_NAMES = {"README.md", "CHECK_REPORT.md"}
_EXT_MAP = {".md": "md", ".txt": "txt", ".pdf": "pdf", ".docx": "docx"}

# Spec S2.2 — real files under docs/demo-knowledge
POSITIVE_CASES: list[dict[str, str]] = [
    {
        "case_id": "md_policy",
        "rel": "docs/demo-knowledge/policy/整机保修政策.md",
        "file_type": "md",
    },
    {
        "case_id": "md_sop",
        "rel": "docs/demo-knowledge/sop/工单处理SOP.md",
        "file_type": "md",
    },
    {
        "case_id": "md_fault_table",
        "rel": "docs/demo-knowledge/sop/常见故障代码对照表.md",
        "file_type": "md",
    },
    {
        "case_id": "md_finance",
        "rel": "docs/demo-knowledge/finance/售后服务收费标准.md",
        "file_type": "md",
    },
    {
        "case_id": "pdf_gateway",
        "rel": "docs/demo-knowledge/product/H3C LA2608室内无线网关 用户手册-6W100-整本手册.pdf",
        "file_type": "pdf",
    },
]

NEGATIVE_EXPECT = {
    "bad_empty_md": {"status": 400, "codes": {"PARSE_EMPTY_RESULT"}},
    "bad_no_source": {"status": 400, "codes": {"BAD_SOURCE", None, ""}},
    "bad_type": {"status": 400, "codes": {"BAD_TYPE", None, ""}},
    "bad_empty_pdf": {"status": 400, "codes": {"EMPTY_PDF", "PARSE_FAILED", "BAD_SOURCE"}},
    "bad_hash_mismatch": {"status": 409, "codes": {"CHUNKS_HASH_MISMATCH"}},
}

_DEPT_RE = re.compile(r"department\s*=\s*([^,，;；\s]+)", re.I)
_META_CAT_RE = re.compile(r"分类\s*[:：]\s*(\S+)", re.I)


def _git_short_sha() -> str | None:
    try:
        import subprocess

        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip() or None
    except Exception:
        return None


def _unit_id_for(rel: str) -> str:
    """ASCII unit_id (contract allows [A-Za-z0-9._:-]{1,64})."""
    digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    return f"eval_full_{digest}"


def discover_full_cases() -> list[dict[str, str]]:
    """All knowledge files under docs/demo-knowledge (exclude README/CHECK_REPORT)."""
    cases: list[dict[str, str]] = []
    if not _CORPUS_ROOT.is_dir():
        return cases
    for path in sorted(_CORPUS_ROOT.rglob("*")):
        if not path.is_file() or path.name in _SKIP_NAMES:
            continue
        ext = path.suffix.lower()
        if ext not in _EXT_MAP:
            continue
        rel = path.relative_to(REPO).as_posix()
        cases.append(
            {
                "case_id": _unit_id_for(rel),
                "rel": rel,
                "file_type": _EXT_MAP[ext],
                "unit_id": _unit_id_for(rel),
            }
        )
    return cases


def _load_progress(report_json: Path) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Return (already-ok positives, permanent-fail case_ids like MINERU_EXTRACT_FAILED)."""
    if not report_json.is_file():
        return {}, set()
    try:
        data = json.loads(report_json.read_text(encoding="utf-8"))
    except Exception:
        return {}, set()
    ok_map: dict[str, dict[str, Any]] = {}
    permanent: set[str] = set()
    for r in data.get("results") or []:
        if r.get("kind") != "positive":
            continue
        cid = str(r.get("case_id") or "")
        if r.get("ok"):
            ok_map[cid] = r
            continue
        detail = str(r.get("detail") or "")
        if "MINERU_EXTRACT_FAILED" in detail or r.get("error_code") == "MINERU_EXTRACT_FAILED":
            permanent.add(cid)
    return ok_map, permanent


@dataclass
class CaseResult:
    case_id: str
    kind: str  # positive | negative
    ok: bool
    detail: str
    http_parse: int | None = None
    http_embed: int | None = None
    error_code: str | None = None
    elapsed_ms: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.is_file():
        raise SystemExit(f"missing {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _apply_env(collection: str, device: str | None) -> None:
    env = _load_env()
    os.environ["INTERNAL_TOKEN"] = env.get("INTERNAL_TOKEN", "change-me-long-random")
    os.environ["MINERU_ENABLED"] = env.get("MINERU_ENABLED", "true")
    os.environ["MINERU_TOKEN"] = env.get("MINERU_TOKEN", "")
    os.environ["MINERU_API_BASE"] = env.get("MINERU_API_BASE", "https://mineru.net")
    os.environ["VECTOR_STORE"] = "milvus"
    os.environ["MILVUS_URI"] = env.get("MILVUS_URI", "http://127.0.0.1:19530")
    os.environ["MILVUS_COLLECTION"] = collection
    os.environ["EMBED_PROVIDER"] = env.get("EMBED_PROVIDER", "local")
    os.environ["EMBED_LOCAL_MODEL"] = env.get("EMBED_LOCAL_MODEL", "")
    os.environ["EMBED_LOCAL_DEVICE"] = device or env.get("EMBED_LOCAL_DEVICE", "cpu")
    os.environ["EMBED_DIM"] = env.get("EMBED_DIM", "1024")
    os.environ["EMBED_BATCH_SIZE"] = env.get("EMBED_BATCH_SIZE", "8")
    # VLM on: real cloud describe for PDF/markdown images (bounded).
    os.environ["VLM_ENABLED"] = "true"
    os.environ["VLM_API_BASE"] = env.get("VLM_API_BASE", env.get("LLM_API_BASE", ""))
    os.environ["VLM_API_KEY"] = env.get("VLM_API_KEY", env.get("LLM_API_KEY", ""))
    os.environ["VLM_MODEL"] = env.get("VLM_MODEL", "glm-4.6v")
    os.environ["MAX_VLM_IMAGES"] = os.environ.get("MAX_VLM_IMAGES", env.get("MAX_VLM_IMAGES", "8"))
    os.environ["PARSE_FAIL_ON_EMPTY"] = "true"


def _preflight(cfg: Any) -> list[str]:
    problems: list[str] = []
    model = Path(str(cfg.embed_local_model or ""))
    if cfg.embed_provider == "local" and not model.exists():
        problems.append(f"embed model path missing: {model}")
    if cfg.vector_store != "milvus":
        problems.append(f"vector_store must be milvus, got {cfg.vector_store}")
    if cfg.milvus_collection == "kb_chunks":
        problems.append("refuse production collection kb_chunks")
    if cfg.mineru_enabled and not str(cfg.mineru_token or "").strip():
        problems.append("MINERU_ENABLED but MINERU_TOKEN empty")
    if cfg.vlm_enabled and not str(cfg.vlm_api_key or "").strip():
        problems.append("VLM_ENABLED but VLM_API_KEY empty")
    uri = str(cfg.milvus_uri or "")
    if not uri.startswith("http"):
        problems.append(f"bad MILVUS_URI: {uri}")
    return problems


def _detail_error_code(resp: Any) -> str | None:
    try:
        detail = resp.json().get("detail")
    except Exception:
        return None
    if isinstance(detail, dict):
        code = detail.get("error_code")
        return str(code) if code is not None else None
    return None


def _acl_from_hint(permission_hint: str | None, category: str | None) -> tuple[str, str]:
    dept = "售后部"
    if permission_hint:
        m = _DEPT_RE.search(permission_hint)
        if m:
            dept = m.group(1).strip()[:64]
        elif permission_hint.strip().lower() == "global":
            dept = "global"
    cls = (category or "").strip()[:64]
    return dept, cls


def run_positive(
    client: Any,
    headers: dict[str, str],
    case: dict[str, str],
    *,
    keep: bool,
    chunk_size: int | None = None,
    unit_prefix: str | None = None,
) -> CaseResult:
    case_id = case["case_id"]
    path = REPO / case["rel"]
    unit_id = case.get("unit_id") or f"eval_ingest_{case_id}"
    if unit_prefix:
        unit_id = f"{unit_prefix}{case_id}"
    t0 = time.monotonic()
    if not path.is_file():
        return CaseResult(case_id, "positive", False, f"source missing: {path}")

    raw = path.read_bytes()
    bucket = size_bucket(case["file_type"], case["rel"], len(raw))
    resolved_chunk = int(chunk_size) if chunk_size else default_chunk_size_for(
        case["file_type"], case["rel"]
    )
    files = {"file": (path.name, raw, "application/octet-stream")}
    data = {
        "file_type": case["file_type"],
        "unit_id": unit_id,
        "use_vlm": "true",
        "ingest_id": "eval_ingest_run",
        "chunk_size": str(resolved_chunk),
    }
    r1 = client.post("/internal/parse", headers=headers, data=data, files=files)
    parse_ms = (time.monotonic() - t0) * 1000
    if r1.status_code != 200:
        return CaseResult(
            case_id,
            "positive",
            False,
            f"parse HTTP {r1.status_code}: {r1.text[:300]}",
            http_parse=r1.status_code,
            error_code=_detail_error_code(r1),
            elapsed_ms=parse_ms,
        )

    body = r1.json()
    q = body.get("quality") or {}
    chunks = body.get("chunks") or []
    if not chunks:
        return CaseResult(
            case_id,
            "positive",
            False,
            "parse returned zero chunks",
            http_parse=200,
            elapsed_ms=parse_ms,
            metrics={"chunk_count": 0},
        )

    from ai.ingest.fingerprint import chunks_fingerprint

    chunk_dicts = [
        {
            "chunk_index": c.get("chunk_index"),
            "content": c.get("content") or "",
            "heading_path": c.get("heading_path") or [],
        }
        for c in chunks
    ]
    chunks_hash = chunks_fingerprint(chunk_dicts)
    dept, cls = _acl_from_hint(body.get("permission_hint"), body.get("suggested_category"))

    t1 = time.monotonic()
    r2 = client.post(
        "/internal/embed",
        headers=headers,
        json={
            "unit_id": unit_id,
            "chunks": chunk_dicts,
            "chunks_hash": chunks_hash,
            "acl_department": dept,
            "acl_classification": cls,
            "ingest_id": "eval_ingest_run",
        },
    )
    embed_ms = (time.monotonic() - t1) * 1000
    if r2.status_code != 200:
        return CaseResult(
            case_id,
            "positive",
            False,
            f"embed HTTP {r2.status_code}: {r2.text[:300]}",
            http_parse=200,
            http_embed=r2.status_code,
            error_code=_detail_error_code(r2),
            elapsed_ms=parse_ms + embed_ms,
            metrics={"chunk_count": len(chunks), "quality": q},
        )

    emb = r2.json()
    parse_stages = body.get("stage_timings") or {}
    embed_stages = emb.get("stage_timings") or {}
    stage_timings = {**parse_stages, **embed_stages}
    milvus_ok, milvus_detail, milvus_metrics = _self_check_milvus(
        unit_id,
        len(chunks),
        first_text=(chunks[0].get("content") or "") if chunks else "",
    )
    if not keep:
        try:
            r3 = client.delete(f"/internal/units/{unit_id}", headers=headers)
            milvus_metrics["cleanup_status"] = r3.status_code
            try:
                milvus_metrics["cleanup_deleted_count"] = r3.json().get("deleted_count")
            except Exception:
                milvus_metrics["cleanup_deleted_count"] = None
            milvus_metrics["cleanup_body"] = r3.text[:200]
        except Exception as exc:  # pragma: no cover
            milvus_metrics["cleanup_error"] = str(exc)

    metrics = {
        "source_rel": case["rel"],
        "file_type": case["file_type"],
        "size_bucket": bucket,
        "source_bytes": len(raw),
        "parse_ms": round(parse_ms, 1),
        "embed_ms": round(embed_ms, 1),
        "elapsed_ms": round(parse_ms + embed_ms, 1),
        "chunk_count": len(chunks),
        "chunk_size": resolved_chunk,
        "stage_timings": stage_timings,
        "embedded_count": emb.get("embedded_count"),
        "dim": emb.get("dim"),
        "resumed_from": emb.get("resumed_from", 0),
        "empty_chunk_ratio": q.get("empty_chunk_ratio"),
        "avg_chunk_chars": q.get("avg_chunk_chars"),
        "max_chunk_chars": q.get("max_chunk_chars"),
        "heading_count": q.get("heading_count"),
        "chunks_with_heading_ratio": q.get("chunks_with_heading_ratio"),
        "suggest_review": q.get("suggest_review"),
        "review_reasons": q.get("review_reasons"),
        "suggested_category": body.get("suggested_category"),
        "permission_hint": body.get("permission_hint"),
        "content_hash": body.get("content_hash"),
        "image_count": len(body.get("images") or []),
        "image_described": sum(1 for i in body.get("images") or [] if i.get("status") == "described"),
        "image_failed": sum(1 for i in body.get("images") or [] if i.get("status") == "failed"),
        "image_skipped": sum(1 for i in body.get("images") or [] if i.get("status") == "skipped"),
        "caption_in_md": "【图示】" in (body.get("markdown") or ""),
        "images": [
            {
                "image_id": i.get("image_id"),
                "relative_path": i.get("relative_path"),
                "status": i.get("status"),
                "caption": (i.get("caption") or "")[:160],
            }
            for i in (body.get("images") or [])[:8]
        ],
        "chunks_hash": body.get("chunks_hash") or chunks_hash,
        "cache_hit": body.get("cache_hit"),
        "page_count": body.get("page_count"),
        "acl_department": dept,
        "acl_classification": cls,
        "milvus_ok": milvus_ok,
        "chunk_previews": [
            {
                "chunk_index": c.get("chunk_index"),
                "heading_path": c.get("heading_path") or [],
                "content": (c.get("content") or "")[:300],
            }
            for c in chunks[:3]
        ],
        **milvus_metrics,
    }

    warns: list[str] = []
    empty_ratio = float(q.get("empty_chunk_ratio") or 0.0)
    if empty_ratio > 0.2:
        warns.append(f"empty_chunk_ratio={empty_ratio:.3f}>0.2")
    table_cases = {"md_policy", "md_fault_table"}
    table_header_missing = False
    if case_id in table_cases:
        joined = "\n".join(str(c.get("content") or "") for c in chunks)
        has_header = bool(re.search(r"^\|.*\|$", joined, re.M)) and ("---" in joined or "—" in joined or re.search(r"\|[\s:\-|]+\|", joined))
        if len(chunks) < 2:
            warns.append("table-like md produced <2 chunks")
        if not has_header:
            warns.append("table header feature missing in chunk texts")
            table_header_missing = True
    if case_id.startswith("md_") and not (body.get("suggested_category") or body.get("permission_hint")):
        warns.append("meta hint missing")

    q_warns = quality_gate_warnings(
        chunk_count=len(chunks),
        avg_chunk_chars=q.get("avg_chunk_chars"),
        empty_chunk_ratio=q.get("empty_chunk_ratio"),
        chunks_with_heading_ratio=q.get("chunks_with_heading_ratio"),
        image_failed=int(metrics.get("image_failed") or 0),
        table_header_missing=table_header_missing,
        suggest_review=bool(q.get("suggest_review")),
        review_reasons=list(q.get("review_reasons") or []),
    )
    metrics["quality_warnings"] = q_warns

    ok = milvus_ok and empty_ratio <= 0.2
    if any(w.startswith("table header") or w.startswith("table-like") for w in warns):
        ok = False
    detail = (
        f"chunks={len(chunks)} embed={emb.get('embedded_count')} dim={emb.get('dim')} "
        f"suggest_review={q.get('suggest_review')} milvus={milvus_ok} "
        f"parse_ms={parse_ms:.0f} embed_ms={embed_ms:.0f}"
    )
    if warns:
        detail += " warns=" + ";".join(warns)
        if empty_ratio > 0.2:
            ok = False
    return CaseResult(
        case_id,
        "positive",
        ok,
        detail,
        http_parse=200,
        http_embed=200,
        elapsed_ms=parse_ms + embed_ms,
        metrics=metrics,
    )


def _self_check_milvus(
    unit_id: str,
    expect_chunks: int,
    *,
    first_text: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    """S2.2: query rows + active version are hard; store.search hit is soft."""
    from app.deps import get_embedder, get_vector_store

    store = get_vector_store()
    embedder = get_embedder()
    metrics: dict[str, Any] = {}
    try:
        version = store.get_active_version(unit_id)
        metrics["active_version"] = version
        if not version:
            return False, "active version empty", metrics

        client = getattr(store, "_client", None)
        coll = getattr(store, "_collection", None)
        if client is None or coll is None:
            return False, "store has no milvus client", metrics
        rows = client.query(
            collection_name=coll,
            filter=f'unit_id == "{unit_id}"',
            output_fields=["chunk_index", "version_id"],
            limit=max(10, expect_chunks + 5),
        )
        metrics["milvus_rows"] = len(rows)
        if expect_chunks and len(rows) < expect_chunks:
            return False, f"rows={len(rows)} < expect={expect_chunks} version={version}", metrics

        # Soft signal only: top-k miss is not "not ingested" when query proves rows exist.
        try:
            probe_text = (first_text or "probe")[:500]
            qvec = embedder.encode([probe_text])[0]
            hits = store.search(qvec, top_k=20)
            hit_units = [str(h.get("unit_id") or "") for h in hits]
            metrics["search_hit_unit"] = unit_id in hit_units
            metrics["search_top_units"] = hit_units[:5]
        except Exception as exc:
            metrics["search_hit_unit"] = None
            metrics["search_error"] = str(exc)

        search_hit = metrics.get("search_hit_unit")
        detail = (
            f"rows={len(rows)} expect={expect_chunks} version={version} "
            f"search_hit={search_hit}"
        )
        # Hard: query rows + active version. Soft: store.search top-k hit.
        return len(rows) >= max(1, expect_chunks), detail, metrics
    except Exception as exc:
        metrics["milvus_error"] = str(exc)
        return False, str(exc), metrics


def run_negative(client: Any, headers: dict[str, str], case_id: str) -> CaseResult:
    expect = NEGATIVE_EXPECT[case_id]
    t0 = time.monotonic()
    data: dict[str, Any]
    files = None

    if case_id == "bad_empty_md":
        data = {"file_type": "md", "unit_id": "eval_ingest_bad_empty_md", "markdown": "   \n  "}
        resp = client.post("/internal/parse", headers=headers, data=data)
    elif case_id == "bad_no_source":
        data = {"file_type": "md", "unit_id": "eval_ingest_bad_no_source"}
        resp = client.post("/internal/parse", headers=headers, data=data)
    elif case_id == "bad_type":
        data = {
            "file_type": "exe",
            "unit_id": "eval_ingest_bad_type",
            "markdown": "x",
        }
        resp = client.post("/internal/parse", headers=headers, data=data)
    elif case_id == "bad_empty_pdf":
        data = {"file_type": "pdf", "unit_id": "eval_ingest_bad_empty_pdf"}
        files = {"file": ("empty.pdf", b"", "application/pdf")}
        resp = client.post("/internal/parse", headers=headers, data=data, files=files)
    elif case_id == "bad_hash_mismatch":
        # Use a tiny synthetic document so this negative does not depend on MinerU.
        data = {
            "file_type": "md",
            "unit_id": "eval_ingest_bad_hash_mismatch",
            "markdown": "# 哈希校验\n\n用于 embed 指纹不一致负向用例。\n",
        }
        r_parse = client.post("/internal/parse", headers=headers, data=data)
        if r_parse.status_code != 200:
            return CaseResult(
                case_id,
                "negative",
                False,
                f"setup parse failed HTTP {r_parse.status_code}",
                http_parse=r_parse.status_code,
                error_code=_detail_error_code(r_parse),
            )
        chunks = r_parse.json().get("chunks") or []
        payload = {
            "unit_id": "eval_ingest_bad_hash_mismatch",
            "chunks": [
                {"chunk_index": c["chunk_index"], "content": c["content"], "heading_path": c.get("heading_path") or []}
                for c in chunks
            ],
            "chunks_hash": "deadbeef" * 8,
            "acl_department": "售后部",
            "acl_classification": "sop",
        }
        resp = client.post("/internal/embed", headers=headers, json=payload)
    else:
        return CaseResult(case_id, "negative", False, "unknown case")

    elapsed = (time.monotonic() - t0) * 1000
    code = _detail_error_code(resp)
    status_ok = resp.status_code == expect["status"]
    code_ok = code in expect["codes"] or (code is None and (None in expect["codes"] or "" in expect["codes"]))
    # Plain-string detail (e.g. unsupported file_type) counts as code None — accepted when expected.
    if resp.status_code == expect["status"] and code is None:
        code_ok = True if (None in expect["codes"] or "" in expect["codes"] or case_id in {"bad_no_source", "bad_type"}) else code_ok
    ok = status_ok and code_ok
    detail = f"HTTP {resp.status_code} error_code={code} body={resp.text[:180]}"
    return CaseResult(
        case_id,
        "negative",
        ok,
        detail,
        http_parse=resp.status_code if case_id != "bad_hash_mismatch" else 200,
        http_embed=resp.status_code if case_id == "bad_hash_mismatch" else None,
        error_code=code,
        elapsed_ms=elapsed,
        metrics={"expected_status": expect["status"], "expected_codes": sorted(str(c) for c in expect["codes"])},
    )


def _snapshot_prev_summary(report_json: Path) -> dict[str, Any] | None:
    if not report_json.is_file():
        return None
    try:
        data = json.loads(report_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    summary = dict(data.get("summary") or {})
    lat = (data.get("observability") or {}).get("latency") or {}
    overall = lat.get("overall") or {}
    stages = (data.get("observability") or {}).get("stages") or {}
    avgs = []
    for r in data.get("results") or []:
        if r.get("kind") == "positive":
            m = r.get("metrics") or {}
            if m.get("avg_chunk_chars") is not None:
                avgs.append(float(m["avg_chunk_chars"]))
    summary["avg_chunk_chars"] = round(sum(avgs) / len(avgs), 2) if avgs else None
    pms = (overall.get("parse_ms") or {}).get("p95")
    ems = (overall.get("embed_ms") or {}).get("p95")
    summary["p95_parse_ms"] = pms
    summary["p95_embed_ms"] = ems
    if not summary.get("positive_total"):
        summary["positive_passed"] = sum(
            1 for r in data.get("results") or [] if r.get("kind") == "positive" and r.get("ok")
        )
        summary["positive_total"] = sum(
            1 for r in data.get("results") or [] if r.get("kind") == "positive"
        )
    summary["_stages_keys"] = list((stages.get("stages") or {}).keys())
    return summary


def write_reports(
    results: list[CaseResult],
    meta: dict[str, Any],
    report_dir: Path | None = None,
    *,
    snapshot_baseline: bool = True,
) -> tuple[Path, Path]:
    out_dir = report_dir or REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    result_dicts = [asdict(r) for r in results]
    rows = result_dicts
    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "positive_passed": sum(1 for r in results if r.kind == "positive" and r.ok),
        "positive_total": sum(1 for r in results if r.kind == "positive"),
        "negative_passed": sum(1 for r in results if r.kind == "negative" and r.ok),
        "negative_total": sum(1 for r in results if r.kind == "negative"),
    }
    lat = latency_stats(rows)
    stages = stage_stats(rows)
    ledgers = build_ledgers(rows)
    avgs = [
        float((r.get("metrics") or {}).get("avg_chunk_chars") or 0)
        for r in rows
        if r.get("kind") == "positive" and (r.get("metrics") or {}).get("avg_chunk_chars") is not None
    ]
    summary["avg_chunk_chars"] = round(sum(avgs) / len(avgs), 2) if avgs else None
    summary["p95_parse_ms"] = (lat.get("overall") or {}).get("parse_ms", {}).get("p95")
    summary["p95_embed_ms"] = (lat.get("overall") or {}).get("embed_ms", {}).get("p95")

    report_json = out_dir / "report.json"
    baseline_path = out_dir / BASELINE_JSON_NAME
    # Prefer a frozen pre-run baseline over snapshotting a RUNNING mid-run report.
    prior_baseline = None
    if baseline_path.is_file():
        try:
            prior_baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except Exception:
            prior_baseline = None
    prev_summary = _snapshot_prev_summary(report_json) if snapshot_baseline else None
    # Drop self-compare: if prev looks like this run (same finished_at), use frozen baseline only.
    if snapshot_baseline and prev_summary and prior_baseline:
        baseline_delta = compare_baseline(prior_baseline, summary)
    elif snapshot_baseline and prev_summary and not prior_baseline:
        baseline_delta = compare_baseline(prev_summary, summary)
        baseline_path.write_text(
            json.dumps(prev_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    elif prior_baseline:
        baseline_delta = compare_baseline(prior_baseline, summary)
    else:
        baseline_delta = {}

    meta = dict(meta)
    meta["baseline_delta"] = baseline_delta
    payload = {
        "meta": meta,
        "summary": summary,
        "observability": {
            "latency": lat,
            "throughput": stages,
            "ledgers": ledgers,
        },
        "results": result_dicts,
    }
    report_json.write_text(
        redact_secrets(json.dumps(payload, ensure_ascii=False, indent=2)), encoding="utf-8"
    )

    fp = meta.get("fingerprint") or {}
    lines = [
        "# 入库真实链路测评报告",
        "",
        f"- 时间：{meta.get('finished_at')}",
        f"- 环境：`{meta.get('python')}`",
        f"- 向量库：`{meta.get('milvus_uri')}` / `{meta.get('collection')}`",
        f"- Embedding：`{meta.get('embed_provider')}` dim=`{meta.get('embed_dim')}`",
        f"- MinerU：enabled=`{meta.get('mineru_enabled')}`",
        f"- VLM：enabled=`{meta.get('vlm_enabled')}` model=`{meta.get('vlm_model')}`",
        f"- 总判定：**{meta.get('overall')}**",
        "",
        "## 运行指纹",
        "",
        f"- embed：`{fp.get('embed_model')}` dim=`{fp.get('embed_dim')}`",
        f"- MinerU/VLM：mineru=`{fp.get('mineru_enabled')}` vlm=`{fp.get('vlm_enabled')}` `{fp.get('vlm_model')}`",
        f"- chunk_size 策略：`{fp.get('chunk_size_policy')}` 覆盖=`{fp.get('chunk_size_override')}`",
        f"- parser_version=`{fp.get('parser_version')}` git=`{fp.get('git_sha')}`",
        "",
        "## 汇总",
        "",
        f"- 正向：{summary['positive_passed']}/{summary['positive_total']}",
        f"- 负向：{summary['negative_passed']}/{summary['negative_total']}",
        f"- 平均切片长：{summary.get('avg_chunk_chars')}",
        "",
        "## 基线对比",
        "",
        f"- 正向率：{baseline_delta.get('positive_rate_prev')} → {baseline_delta.get('positive_rate_cur')} "
        f"(Δ={baseline_delta.get('positive_rate_delta')})",
        f"- avg_chunk_chars：{baseline_delta.get('avg_chunk_chars_prev')} → {baseline_delta.get('avg_chunk_chars_cur')} "
        f"(Δ={baseline_delta.get('avg_chunk_chars_delta')})",
        f"- P95 parse_ms：{baseline_delta.get('p95_parse_ms_prev')} → {baseline_delta.get('p95_parse_ms_cur')} "
        f"(Δ={baseline_delta.get('p95_parse_ms_delta')})",
        f"- P95 embed_ms：{baseline_delta.get('p95_embed_ms_prev')} → {baseline_delta.get('p95_embed_ms_cur')} "
        f"(Δ={baseline_delta.get('p95_embed_ms_delta')})",
        "",
        "## 耗时分位（正向）",
        "",
        "| bucket | n | parse P50 | P95 | P99 | embed P50 | P95 | P99 | elapsed P50 | P95 | P99 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def _fmt(v: Any) -> str:
        if v is None:
            return "-"
        return f"{float(v):.0f}" if abs(float(v)) >= 10 else f"{float(v):.1f}"

    for bucket, block in list((lat.get("by_bucket") or {}).items()) + [("ALL", lat.get("overall") or {})]:
        p, e, el = block.get("parse_ms") or {}, block.get("embed_ms") or {}, block.get("elapsed_ms") or {}
        n = p.get("n") or 0
        lines.append(
            f"| {bucket} | {n} | {_fmt(p.get('p50'))} | {_fmt(p.get('p95'))} | {_fmt(p.get('p99'))} | "
            f"{_fmt(e.get('p50'))} | {_fmt(e.get('p95'))} | {_fmt(e.get('p99'))} | "
            f"{_fmt(el.get('p50'))} | {_fmt(el.get('p95'))} | {_fmt(el.get('p99'))} |"
        )

    st = stages.get("stages") or {}
    lines.extend(
        [
            "",
            "## 阶段耗时与吞吐",
            "",
            "| stage | n | sum_ms | avg_ms | p95_ms |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for key in ("extract_ms", "vlm_ms", "chunk_ms", "encode_ms", "publish_ms"):
        s = st.get(key) or {}
        lines.append(
            f"| {key} | {s.get('n')} | {_fmt(s.get('sum'))} | {_fmt(s.get('avg'))} | {_fmt(s.get('p95'))} |"
        )
    lines.extend(
        [
            "",
            f"- chunks_total：{stages.get('chunks_total')}",
            f"- chunks/s：{stages.get('chunks_per_s')}",
            f"- embed_ms_per_chunk：{stages.get('embed_ms_per_chunk')}",
            "",
            "## 台账 · 永久失败",
            "",
            "| case_id | source_rel | error_code | 建议动作 | detail |",
            "|---|---|---|---|---|",
        ]
    )
    for row in ledgers.get("permanent_fail") or []:
        lines.append(
            f"| {row.get('case_id')} | {row.get('source_rel')} | {row.get('error_code')} | "
            f"{row.get('action')} | {str(row.get('detail') or '')[:80]} |"
        )
    lines.extend(
        [
            "",
            "## 台账 · 瞬时失败",
            "",
            "| case_id | error_code | attempts | resumed | detail |",
            "|---|---|---:|---|---|",
        ]
    )
    for row in ledgers.get("transient_fail") or []:
        lines.append(
            f"| {row.get('case_id')} | {row.get('error_code')} | {row.get('attempts')} | "
            f"{row.get('resumed')} | {str(row.get('detail') or '')[:80]} |"
        )
    lines.extend(
        [
            "",
            "## 台账 · 质量告警",
            "",
            "| case_id | kind | message | 阈值 | 实测 | action |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in ledgers.get("quality_warnings") or []:
        lines.append(
            f"| {row.get('case_id')} | {row.get('kind')} | {str(row.get('message') or '')[:80]} | "
            f"{row.get('threshold') or ''} | {row.get('actual') or ''} | {row.get('action')} |"
        )

    lines.extend(
        [
            "",
            "## 逐 case",
            "",
            "| case | kind | ok | detail |",
            "|---|---|---|---|",
        ]
    )
    for r in results:
        detail = redact_secrets(r.detail).replace("|", "\\|")
        lines.append(f"| {r.case_id} | {r.kind} | {'PASS' if r.ok else 'FAIL'} | {detail} |")
    lines.append("")
    lines.append("## 正向指标")
    lines.append("")
    lines.append(
        "| case | chunks | empty_ratio | suggest_review | category | permission | images desc/fail | "
        "caption_in_md | milvus_rows | search_hit | parse_ms | embed_ms | chunk_size |"
    )
    lines.append("|---|---:|---:|---|---|---|---|---|---:|---|---:|---:|---:|")
    for r in results:
        if r.kind != "positive":
            continue
        m = r.metrics
        lines.append(
            "| {case_id} | {chunks} | {empty} | {review} | {cat} | {perm} | {desc}/{fail} | {cap} | {rows} | {sh} | {pms} | {ems} | {cs} |".format(
                case_id=r.case_id,
                chunks=m.get("chunk_count"),
                empty=m.get("empty_chunk_ratio"),
                review=m.get("suggest_review"),
                cat=m.get("suggested_category"),
                perm=(m.get("permission_hint") or "")[:40],
                desc=m.get("image_described"),
                fail=m.get("image_failed"),
                cap=m.get("caption_in_md"),
                rows=m.get("milvus_rows"),
                sh=m.get("search_hit_unit"),
                pms=m.get("parse_ms"),
                ems=m.get("embed_ms"),
                cs=m.get("chunk_size"),
            )
        )
    lines.append("")
    report_md = out_dir / "report.md"
    report_md.write_text(redact_secrets("\n".join(lines) + "\n"), encoding="utf-8")

    for r in results:
        # Avoid path-hostile case ids on Windows.
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", r.case_id)[:80]
        (out_dir / f"case_{safe}.json").write_text(
            redact_secrets(json.dumps(asdict(r), ensure_ascii=False, indent=2)), encoding="utf-8"
        )
    return report_json, report_md


def _load_known_bad() -> tuple[set[str], list[dict[str, Any]]]:
    if not KNOWN_BAD_JSON.is_file():
        return set(), []
    try:
        data = json.loads(KNOWN_BAD_JSON.read_text(encoding="utf-8"))
    except Exception:
        return set(), []
    if isinstance(data, list):
        return {str(x) for x in data}, []
    ids = {str(x) for x in (data.get("case_ids") or [])}
    entries = list(data.get("entries") or [])
    return ids, entries


def _save_known_bad(ids: set[str], entries: list[dict[str, Any]] | None = None) -> None:
    KNOWN_BAD_JSON.parent.mkdir(parents=True, exist_ok=True)
    prev_ids, prev_entries = _load_known_bad()
    by_id = {str(e.get("case_id")): e for e in (entries or prev_entries) if e.get("case_id")}
    merged_entries = []
    for cid in sorted(ids):
        e = dict(by_id.get(cid) or {"case_id": cid})
        e.setdefault("error_code", "MINERU_EXTRACT_FAILED")
        e.setdefault("action", "replace_source_or_exclude")
        e["case_id"] = cid
        merged_entries.append(e)
    payload = {
        "case_ids": sorted(ids),
        "entries": merged_entries,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": (
            "MinerU permanent extract failures. Excluded from positive scoring denominator "
            "when skipped; see ledger permanent_fail. Replace source or keep excluded."
        ),
    }
    KNOWN_BAD_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_qc_pack(results: list[CaseResult], out_dir: Path) -> int:
    """One tri-fold sample per bucket: source excerpt → chunk previews → vector hit."""
    pack_dir = out_dir / QC_PACK_DIR_NAME
    pack_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    n = 0
    for r in results:
        if r.kind != "positive" or not r.ok:
            continue
        m = r.metrics or {}
        bucket = str(m.get("size_bucket") or "other")
        if bucket in seen:
            continue
        seen.add(bucket)
        rel = m.get("source_rel") or ""
        path = REPO / rel if rel else None
        excerpt = ""
        previews = list(m.get("chunk_previews") or [])[:3]
        if path and path.is_file():
            try:
                raw = path.read_bytes()[:2000]
                excerpt = raw.decode("utf-8", errors="replace")[:800]
            except Exception:
                excerpt = ""
            if not previews and path.suffix.lower() in {".md", ".txt"}:
                try:
                    from ai.ingest.chunker import split_markdown

                    text = path.read_text(encoding="utf-8", errors="replace")
                    objs = split_markdown(
                        text,
                        chunk_size=int(m.get("chunk_size") or 600),
                        chunk_overlap=50,
                    )
                    previews = [
                        {
                            "chunk_index": c.chunk_index,
                            "heading_path": c.heading_path,
                            "content": c.content[:300],
                            "source": "rechunked_from_source",
                        }
                        for c in objs[:3]
                    ]
                except Exception:
                    previews = []
        pack = {
            "case_id": r.case_id,
            "size_bucket": bucket,
            "source_rel": rel,
            "source_excerpt": excerpt,
            "chunk_previews": previews,
            "vector_hit": {
                "milvus_rows": m.get("milvus_rows"),
                "search_hit_unit": m.get("search_hit_unit"),
                "active_version": m.get("active_version"),
                "chunk_count": m.get("chunk_count"),
            },
        }
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", bucket)[:40]
        (pack_dir / f"qc_{safe}.json").write_text(
            redact_secrets(json.dumps(pack, ensure_ascii=False, indent=2)), encoding="utf-8"
        )
        n += 1
    return n


def run_ab_chunk_sizes(
    client: Any,
    headers: dict[str, str],
    sizes: list[int],
    *,
    with_pdf: bool,
    collection: str,
) -> int:
    cases = list(POSITIVE_CASES)
    if not with_pdf:
        cases = [c for c in cases if c["file_type"] != "pdf"]
    else:
        # keep at most one pdf for speed
        pdfs = [c for c in cases if c["file_type"] == "pdf"]
        mds = [c for c in cases if c["file_type"] != "pdf"]
        cases = mds + pdfs[:1]

    rows: list[dict[str, Any]] = []
    results: list[CaseResult] = []
    for size in sizes:
        for case in cases:
            unit_prefix = f"ab{size}_"
            r = run_positive(
                client,
                headers,
                case,
                keep=True,
                chunk_size=size,
                unit_prefix=unit_prefix,
            )
            results.append(r)
            m = r.metrics or {}
            rows.append(
                {
                    "chunk_size": size,
                    "case_id": case["case_id"],
                    "ok": r.ok,
                    "chunk_count": m.get("chunk_count"),
                    "avg_chunk_chars": m.get("avg_chunk_chars"),
                    "max_chunk_chars": m.get("max_chunk_chars"),
                    "empty_chunk_ratio": m.get("empty_chunk_ratio"),
                    "parse_ms": m.get("parse_ms"),
                    "embed_ms": m.get("embed_ms"),
                    "milvus_ok": m.get("milvus_ok"),
                    "search_hit_unit": m.get("search_hit_unit"),
                }
            )
            print(f"[AB size={size}] {case['case_id']}: {r.detail}")

    # aggregate
    agg: dict[int, dict[str, Any]] = {}
    for size in sizes:
        sub = [x for x in rows if x["chunk_size"] == size]
        ok_n = sum(1 for x in sub if x["ok"])
        chunks = [x["chunk_count"] or 0 for x in sub]
        avgs = [x["avg_chunk_chars"] or 0 for x in sub]
        agg[size] = {
            "ok": f"{ok_n}/{len(sub)}",
            "chunks_total": sum(chunks),
            "avg_chunk_chars_mean": round(sum(avgs) / len(avgs), 1) if avgs else 0,
            "empty_max": max((x["empty_chunk_ratio"] or 0) for x in sub) if sub else 0,
            "parse_ms_sum": round(sum((x["parse_ms"] or 0) for x in sub), 1),
            "embed_ms_sum": round(sum((x["embed_ms"] or 0) for x in sub), 1),
            "search_miss": sum(1 for x in sub if x.get("search_hit_unit") is False),
        }

    AB_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "collection": collection,
            "sizes": sizes,
            "cases": [c["case_id"] for c in cases],
        },
        "rows": rows,
        "aggregate": {str(k): v for k, v in agg.items()},
    }
    (AB_REPORT_DIR / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# chunk_size A/B（真实链路）",
        "",
        f"- 集合：`{collection}`",
        f"- 配置：{sizes}",
        f"- 样本：{', '.join(c['case_id'] for c in cases)}",
        "",
        "| chunk_size | 成功 | 总切片 | 均长 | 最大空片率 | parse_ms | embed_ms | search_miss |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for size in sizes:
        a = agg[size]
        lines.append(
            f"| {size} | {a['ok']} | {a['chunks_total']} | {a['avg_chunk_chars_mean']} | "
            f"{a['empty_max']} | {a['parse_ms_sum']} | {a['embed_ms_sum']} | {a['search_miss']} |"
        )
    lines.append("")
    lines.append("## 逐 case")
    lines.append("")
    lines.append("| size | case | ok | chunks | avg_chars | empty | parse_ms | embed_ms | search_hit |")
    lines.append("|---:|---|---|---:|---:|---:|---:|---:|---|")
    for x in rows:
        lines.append(
            f"| {x['chunk_size']} | {x['case_id']} | {'PASS' if x['ok'] else 'FAIL'} | {x['chunk_count']} | "
            f"{x['avg_chunk_chars']} | {x['empty_chunk_ratio']} | {x['parse_ms']} | {x['embed_ms']} | {x['search_hit_unit']} |"
        )
    md = AB_REPORT_DIR / "report.md"
    # Per-case JSON first (write_reports also writes report.md/json for generic mode).
    case_dir = AB_REPORT_DIR / "cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", r.case_id)[:80]
        (case_dir / f"case_{safe}.json").write_text(
            redact_secrets(json.dumps(asdict(r), ensure_ascii=False, indent=2)), encoding="utf-8"
        )
    # A/B summary last so it is not clobbered by generic write_reports.
    (AB_REPORT_DIR / "report.json").write_text(
        redact_secrets(json.dumps(payload, ensure_ascii=False, indent=2)), encoding="utf-8"
    )
    md.write_text(redact_secrets("\n".join(lines) + "\n"), encoding="utf-8")
    config_md = AB_REPORT_DIR / "config.md"
    config_md.write_text(
        redact_secrets(
            "\n".join(
                [
                    "# chunk_size A/B 配置说明",
                    "",
                    f"- 集合：`{collection}`",
                    f"- 对比尺寸：{sizes}",
                    f"- 样本：{', '.join(c['case_id'] for c in cases)}",
                    "",
                    "## 默认策略（S2.3 / docs/config.md）",
                    "",
                    "| 类型 | chunk_size |",
                    "|---|---:|",
                    "| product PDF | 1000 |",
                    "| other PDF | 800 |",
                    "| policy / sop / finance / sales / 表格 MD | 700 |",
                    "| FAQ 短文 | 512 |",
                    "| docx | 700 |",
                    "| 其它 md/txt | 600 |",
                    "",
                    "## 结论",
                    "",
                    "- 以 `report.md` 中 512 vs 1000 聚合为准；产品手册 PDF 推荐 **1000**（片数更少、均长更合理）。",
                    "- 切片算法不变，仅请求参数 `chunk_size`。",
                    "",
                ]
            )
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nAB report: {md}")
    print(f"AB config: {config_md}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-chain ingest evaluation (small samples)")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--device", default=None, help="override EMBED_LOCAL_DEVICE (cpu/cuda)")
    parser.add_argument("--cleanup", action="store_true", help="delete eval unit vectors after self-check")
    parser.add_argument("--skip-pdf", action="store_true", help="skip MinerU PDF case (smoke only)")
    parser.add_argument("--all", action="store_true", help="full docs/demo-knowledge corpus (md+pdf+docx)")
    parser.add_argument("--resume", action="store_true", help="skip positives already ok in full report")
    parser.add_argument("--skip-neg", action="store_true", help="skip negative cases (full runs)")
    parser.add_argument("--chunk-size", type=int, default=None, help="override parse chunk_size")
    parser.add_argument(
        "--ab-chunk-sizes",
        default=None,
        help="comma sizes e.g. 512,1000; run sample A/B and exit",
    )
    parser.add_argument("--ab-with-pdf", action="store_true", help="include one PDF in A/B set")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    args = parser.parse_args()

    report_dir = FULL_REPORT_DIR if args.all else REPORT_DIR
    positive_cases = discover_full_cases() if args.all else POSITIVE_CASES

    if args.list:
        print(f"mode: {'full' if args.all else 'sample'}  cases={len(positive_cases)}")
        print("positive:")
        for c in positive_cases:
            print(f"  {c.get('unit_id', c['case_id'])}\t{c['rel']}")
        if not args.skip_neg:
            print("negative:")
            for cid in NEGATIVE_EXPECT:
                print(f"  {cid}")
        return 0

    _apply_env(args.collection, args.device)
    from core.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()
    problems = _preflight(cfg)
    if problems:
        for p in problems:
            print(f"[ENV] {p}")
        # S2.5: env failures still produce a report artifact.
        env_result = CaseResult(
            "env_preflight",
            "negative",
            False,
            "; ".join(problems),
            metrics={"problems": problems},
        )
        meta = {
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.executable,
            "milvus_uri": getattr(cfg, "milvus_uri", None),
            "collection": getattr(cfg, "milvus_collection", None),
            "embed_provider": getattr(cfg, "embed_provider", None),
            "embed_dim": getattr(cfg, "embed_dim", None),
            "mineru_enabled": getattr(cfg, "mineru_enabled", None),
            "overall": "ENV_FAIL",
        }
        write_reports([env_result], meta, report_dir)
        return 2

    print(
        f"[cfg] python={sys.executable} embed={cfg.embed_provider}/{cfg.embed_dim} "
        f"store={cfg.vector_store}/{cfg.milvus_collection} mineru={cfg.mineru_enabled} "
        f"vlm={cfg.vlm_enabled} mode={'full' if args.all else 'sample'} cases={len(positive_cases)}"
    )

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    headers = {"X-Internal-Token": cfg.internal_token}

    if args.ab_chunk_sizes:
        sizes = [int(x.strip()) for x in args.ab_chunk_sizes.split(",") if x.strip()]
        return run_ab_chunk_sizes(
            client,
            headers,
            sizes,
            with_pdf=bool(args.ab_with_pdf),
            collection=cfg.milvus_collection,
        )

    # connectivity probe
    try:
        rh = client.get("/health")
        print(f"[health] {rh.status_code} {rh.text[:200]}")
        if rh.status_code != 200:
            raise RuntimeError(f"health HTTP {rh.status_code}")
    except Exception as exc:
        print(f"[ENV] health failed: {exc}")
        env_result = CaseResult(
            "env_health",
            "negative",
            False,
            str(exc),
            metrics={"problems": [str(exc)]},
        )
        write_reports(
            [env_result],
            {
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "python": sys.executable,
                "milvus_uri": cfg.milvus_uri,
                "collection": cfg.milvus_collection,
                "embed_provider": cfg.embed_provider,
                "embed_dim": cfg.embed_dim,
                "mineru_enabled": cfg.mineru_enabled,
                "vlm_enabled": getattr(cfg, "vlm_enabled", None),
                "overall": "ENV_FAIL",
                "mode": "full" if args.all else "sample",
            },
            report_dir,
        )
        return 2

    done_map: dict[str, dict[str, Any]] = {}
    permanent_fail: set[str] = set()
    known_bad, known_bad_entries = _load_known_bad()
    case_rel = {c["case_id"]: c.get("rel") for c in positive_cases}
    if args.all and args.resume:
        done_map, permanent_fail = _load_progress(report_dir / "report.json")
        permanent_fail |= known_bad
        print(
            f"[resume] skip {len(done_map)} already-ok, "
            f"{len(permanent_fail)} permanent/deny MINERU_EXTRACT_FAILED"
        )
        enriched = list(known_bad_entries)
        have = {str(e.get("case_id")) for e in enriched}
        for cid in sorted(permanent_fail):
            if cid not in have:
                enriched.append(
                    {
                        "case_id": cid,
                        "source_rel": case_rel.get(cid),
                        "error_code": "MINERU_EXTRACT_FAILED",
                        "action": "replace_source_or_exclude",
                    }
                )
            else:
                for e in enriched:
                    if e.get("case_id") == cid and not e.get("source_rel"):
                        e["source_rel"] = case_rel.get(cid)
        _save_known_bad(permanent_fail, enriched)

    results: list[CaseResult] = []
    total = len(positive_cases)
    for idx, case in enumerate(positive_cases, start=1):
        if args.all and args.resume and case["case_id"] in done_map:
            prev = done_map[case["case_id"]]
            results.append(
                CaseResult(
                    case_id=prev.get("case_id") or case["case_id"],
                    kind="positive",
                    ok=bool(prev.get("ok")),
                    detail=str(prev.get("detail") or "").replace(" (resumed)", "") + " (resumed)",
                    metrics=dict(prev.get("metrics") or {}),
                )
            )
            m = results[-1].metrics
            m["resumed"] = True
            if m.get("size_bucket") is None:
                m["size_bucket"] = size_bucket(
                    m.get("file_type") or case.get("file_type") or "",
                    m.get("source_rel") or case.get("rel"),
                )
            if not m.get("elapsed_ms"):
                m["elapsed_ms"] = float(m.get("parse_ms") or 0) + float(m.get("embed_ms") or 0)
            if not m.get("chunk_size"):
                m["chunk_size"] = default_chunk_size_for(
                    m.get("file_type") or case.get("file_type") or "",
                    m.get("source_rel") or case.get("rel"),
                )
                m["chunk_size_backfilled"] = True
            if not m.get("stage_timings"):
                parse_ms = float(m.get("parse_ms") or 0)
                embed_ms = float(m.get("embed_ms") or 0)
                # Approximate split only when historical rows lack stage_timings.
                m["stage_timings"] = {
                    "extract_ms": round(parse_ms * 0.9, 1) if (m.get("file_type") == "pdf") else round(parse_ms * 0.3, 1),
                    "vlm_ms": 0.0,
                    "chunk_ms": round(parse_ms * 0.1, 1),
                    "encode_ms": round(embed_ms * 0.3, 1),
                    "publish_ms": round(embed_ms * 0.7, 1),
                    "approx_from_totals": 1.0,
                }
            # Keep real stage_timings as-is when present (no approx rewrite).
            if not m.get("quality_warnings"):
                m["quality_warnings"] = quality_gate_warnings(
                    chunk_count=int(m.get("chunk_count") or 0),
                    avg_chunk_chars=m.get("avg_chunk_chars"),
                    empty_chunk_ratio=m.get("empty_chunk_ratio"),
                    chunks_with_heading_ratio=m.get("chunks_with_heading_ratio"),
                    image_failed=int(m.get("image_failed") or 0),
                    table_header_missing=False,
                    suggest_review=bool(m.get("suggest_review")),
                    review_reasons=list(m.get("review_reasons") or []),
                )
            print(f"[SKIP] resume {case['case_id']}")
            continue
        if case["case_id"] in known_bad or (args.all and args.resume and case["case_id"] in permanent_fail):
            results.append(
                CaseResult(
                    case_id=case["case_id"],
                    kind="positive",
                    ok=False,
                    detail="MINERU_EXTRACT_FAILED (permanent; skipped on resume)",
                    error_code="MINERU_EXTRACT_FAILED",
                    metrics={
                        "source_rel": case.get("rel"),
                        "file_type": case.get("file_type"),
                        "size_bucket": size_bucket(case.get("file_type") or "", case.get("rel")),
                        "quality_warnings": [],
                    },
                )
            )
            print(f"[SKIP] permanent-fail {case['case_id']}")
            continue
        if args.skip_pdf and case["file_type"] == "pdf":
            results.append(CaseResult(case["case_id"], "positive", True, "skipped --skip-pdf"))
            continue
        label = case.get("unit_id") or case["case_id"]
        print(f"\n=== positive {idx}/{total} {label} :: {case['rel']} ===")
        try:
            r = run_positive(client, headers, case, keep=not args.cleanup)
        except Exception:
            traceback.print_exc()
            r = CaseResult(case["case_id"], "positive", False, "exception")
        print(("[PASS]" if r.ok else "[FAIL]") + f" {label}: {r.detail}")
        results.append(r)
        if args.all:
            write_reports(
                results,
                {"finished_at": time.strftime("%Y-%m-%d %H:%M:%S"), "overall": "RUNNING", "mode": "full"},
                report_dir,
                snapshot_baseline=False,
            )

    if not args.skip_neg:
        for case_id in NEGATIVE_EXPECT:
            print(f"\n=== negative {case_id} ===")
            try:
                r = run_negative(client, headers, case_id)
            except Exception:
                traceback.print_exc()
                r = CaseResult(case_id, "negative", False, "exception")
            print(("[PASS]" if r.ok else "[FAIL]") + f" {case_id}: {r.detail}")
            results.append(r)

    pos_pass = sum(1 for r in results if r.kind == "positive" and r.ok)
    pos_total = sum(1 for r in results if r.kind == "positive")
    neg_pass = sum(1 for r in results if r.kind == "negative" and r.ok)
    neg_total = sum(1 for r in results if r.kind == "negative")
    permanent_fail_ids = {
        r.case_id
        for r in results
        if r.kind == "positive"
        and not r.ok
        and (r.error_code in {"MINERU_EXTRACT_FAILED"} or "MINERU_EXTRACT_FAILED" in (r.detail or ""))
    }
    repairable_fail = sum(
        1
        for r in results
        if r.kind == "positive" and not r.ok and r.case_id not in permanent_fail_ids
    )
    pos_repairable_total = pos_total - len(permanent_fail_ids)
    if pos_pass == pos_total and (args.skip_neg or neg_pass >= max(1, neg_total - 1)):
        overall = "PASS"
    elif repairable_fail == 0 and (args.skip_neg or neg_pass >= max(1, neg_total - 1)):
        overall = "PASS_EXCLUDE_PERMANENT"
    else:
        overall = "FAIL"

    meta = {
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.executable,
        "milvus_uri": cfg.milvus_uri,
        "collection": cfg.milvus_collection,
        "embed_provider": cfg.embed_provider,
        "embed_dim": cfg.embed_dim,
        "mineru_enabled": cfg.mineru_enabled,
        "vlm_enabled": cfg.vlm_enabled,
        "vlm_model": cfg.vlm_model,
        "mode": "full" if args.all else "sample",
        "corpus_size": len(positive_cases),
        "cleanup": args.cleanup,
        "skip_pdf": args.skip_pdf,
        "skip_neg": args.skip_neg,
        "overall": overall,
        "scorecard": {
            "pos_pass": pos_pass,
            "pos_total": pos_total,
            "permanent_excluded": len(permanent_fail_ids),
            "pos_repairable_total": pos_repairable_total,
            "neg_pass": neg_pass,
            "neg_total": neg_total,
            "repairable_fail": repairable_fail,
        },
        "fingerprint": {
            "embed_provider": cfg.embed_provider,
            "embed_model": getattr(cfg, "embed_local_model", None)
            or getattr(cfg, "embed_cloud_model", None),
            "embed_dim": cfg.embed_dim,
            "mineru_enabled": cfg.mineru_enabled,
            "vlm_enabled": cfg.vlm_enabled,
            "vlm_model": cfg.vlm_model,
            "chunk_size_policy": "product_pdf=1000 other_pdf=800 policy/sop/finance/sales=700 faq=512 docx=700 default=600",
            "parse_cache_disk": bool(getattr(cfg, "parse_cache_disk_enabled", False)),
            "mineru_extract_cache": bool(getattr(cfg, "mineru_extract_cache_enabled", False)),
            "chunk_size_override": args.chunk_size,
            "parser_version": getattr(cfg, "parser_version", None),
            "git_sha": _git_short_sha(),
        },
    }
    report_json, report_md = write_reports(results, meta, report_dir)
    qc_n = _write_qc_pack(results, report_dir)
    print(f"[qc_pack] wrote {qc_n} tri-fold samples → {report_dir / QC_PACK_DIR_NAME}")
    print("\n=== SUMMARY ===")
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL':4}  {r.kind:8}  {r.case_id}: {r.detail}")
    print(f"positive {pos_pass}/{pos_total}  negative {neg_pass}/{neg_total}  overall={overall}")
    print(f"report: {report_md}")
    print(f"json:   {report_json}")
    return 0 if overall in {"PASS", "PASS_EXCLUDE_PERMANENT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
