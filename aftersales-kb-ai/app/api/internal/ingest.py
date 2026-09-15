"""Ingest /internal routes: parse, async tasks, embed, delete."""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core.config import Settings, embed_model_id, get_settings
from app.deps import get_embedder, get_mineru, get_vector_store, get_vlm
from schemas.ingest import (
    DeleteUnitResponse,
    EmbedRequest,
    EmbedResponse,
    ParseResponse,
    ParseTaskAccepted,
    ParseTaskStatus,
)
from ai.ingest.chunker import enrich_chunk_for_embed, split_markdown
from ai.ingest.content_cache import content_hash, get_parse_cache
from ai.embed.embedder import Embedder
from ai.ingest.fingerprint import chunks_fingerprint, content_fingerprint
from core.limits import get_limits
from ai.ingest.mineru_client import MinerUClient
from ai.ingest.parse import ParsePipelineError, run_parse
from ai.ingest.task_store import get_task_store
from ai.retrieve.lexical import get_lexical_index
from ai.retrieve.store import PublishValidationError, VectorItem, VectorStore, _stable_id
from ai.generate.vlm import VlmClient, VlmError
from core.circuit import get_circuits, get_metrics
from core.errors import detail_body, safe_message, sanitize_text
from core.retry import RetryPolicy, retry_call
from core.trace import get_trace_id, set_trace_id
from core.unit_lock import get_unit_locks

from app.api.internal._common import (
    _ALLOWED_TYPES,
    _UNIT_ID_RE,
    _chunk_params,
    _enforce_markdown_limit,
    _get_parse_sem,
    _max_msg,
    _normalize_file_type,
    _parse_pool,
    _parse_spool_dir,
    _require_source,
    _require_unit_id,
    _spool_upload,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-ingest"])

@router.post("/parse", response_model=ParseResponse)
async def parse(
    file: UploadFile | None = File(default=None),
    markdown: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    source_name: str | None = Form(default=None),
    file_type: str = Form(...),
    unit_id: str = Form(...),
    chunk_size: int | None = Form(default=None),
    chunk_overlap: int | None = Form(default=None),
    use_vlm: bool = Form(default=False),
    ingest_id: str | None = Form(default=None),
    cfg: Settings = Depends(get_settings),
    mineru: MinerUClient = Depends(get_mineru),
    vlm: VlmClient = Depends(get_vlm),
) -> ParseResponse:
    """Synchronous parse. Production main path is this + Java orchestration."""
    ft = _normalize_file_type(file_type)
    uid = _require_unit_id(unit_id)
    size, overlap = _chunk_params(cfg, chunk_size, chunk_overlap)
    _enforce_markdown_limit(markdown, cfg)
    _require_source(file=file, markdown=markdown, source_url=source_url, file_type=ft)
    if ingest_id is not None and len(ingest_id) > 128:
        raise HTTPException(status_code=400, detail="ingest_id max_length=128")

    file_bytes: bytes | None = None
    file_path: str | None = None
    spool_path: Path | None = None
    if file is not None:
        file_bytes, spool_path = await _spool_upload(file, cfg)
        if spool_path is not None:
            file_path = str(spool_path)
    cache = get_parse_cache(cfg.parse_cache_max)
    cache_key: str | None = None
    if cfg.enable_parse_idempotency and source_url is None and file_bytes is not None:
        # Only hash in-memory uploads; large spooled files skip cache to avoid re-read.
        cache_key = content_hash(
            file_type=ft,
            file_bytes=file_bytes,
            markdown=markdown,
            chunk_size=size,
            chunk_overlap=overlap,
            use_vlm=use_vlm,
            parser_version=cfg.parser_version,
            embed_model=embed_model_id(cfg),
            config_version=cfg.cache_config_version,
        )
        hit = cache.get(cache_key)
        if hit is not None:
            if spool_path is not None:
                spool_path.unlink(missing_ok=True)
            payload = dict(hit)
            payload["unit_id"] = uid
            payload["cache_hit"] = True
            payload["ingest_id"] = ingest_id or None
            if not payload.get("content_hash"):
                payload["content_hash"] = content_fingerprint(payload.get("markdown") or "")
            if not payload.get("chunks_hash"):
                payload["chunks_hash"] = chunks_fingerprint(payload.get("chunks") or [])
            return ParseResponse(**payload)
    elif cfg.enable_parse_idempotency and source_url is None and file is None:
        cache_key = content_hash(
            file_type=ft,
            file_bytes=None,
            markdown=markdown,
            chunk_size=size,
            chunk_overlap=overlap,
            use_vlm=use_vlm,
            parser_version=cfg.parser_version,
            embed_model=embed_model_id(cfg),
            config_version=cfg.cache_config_version,
        )
        hit = cache.get(cache_key)
        if hit is not None:
            payload = dict(hit)
            payload["unit_id"] = uid
            payload["cache_hit"] = True
            payload["ingest_id"] = ingest_id or None
            if not payload.get("content_hash"):
                payload["content_hash"] = content_fingerprint(payload.get("markdown") or "")
            if not payload.get("chunks_hash"):
                payload["chunks_hash"] = chunks_fingerprint(payload.get("chunks") or [])
            return ParseResponse(**payload)

    timed_out = False
    pool: ThreadPoolExecutor | None = None
    try:
        timeout_sec = float(cfg.parse_overall_timeout_sec or 0)
        parse_kwargs = dict(
            unit_id=uid,
            file_type=ft,
            file_bytes=file_bytes,
            file_path=file_path,
            file_name=file.filename if file else None,
            markdown=markdown,
            source_url=source_url,
            source_name=source_name,
            chunk_size=size,
            chunk_overlap=overlap,
            use_vlm=use_vlm,
            cfg=cfg,
            mineru=mineru,
            vlm=vlm,
            ingest_id=ingest_id,
        )
        if timeout_sec > 0:
            # Wall-clock budget for Java orchestration. On timeout we must NOT
            # block the request on the abandoned worker; spool is cleaned after
            # that worker finishes (see background cleanup below).
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync-parse")
            fut = pool.submit(run_parse, **parse_kwargs)
            try:
                result = fut.result(timeout=timeout_sec)
            except FuturesTimeoutError as exc:
                timed_out = True
                get_metrics().inc("parse.timeout")
                if spool_path is not None:
                    cleanup_path = spool_path
                    cleanup_fut = fut

                    def _cleanup_spool_after_worker() -> None:
                        try:
                            cleanup_fut.result()
                        except Exception:
                            pass
                        finally:
                            cleanup_path.unlink(missing_ok=True)

                    threading.Thread(
                        target=_cleanup_spool_after_worker,
                        name="parse-spool-clean",
                        daemon=True,
                    ).start()
                    spool_path = None
                raise ParsePipelineError(
                    f"parse exceeded parse_overall_timeout_sec={timeout_sec}",
                    "PARSE_TIMEOUT",
                    504,
                ) from exc
        else:
            result = run_parse(**parse_kwargs)
        if cache_key is not None:
            cache.put(cache_key, result.model_dump())
        return result
    except ParsePipelineError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail=detail_body(
                exc.error_code,
                str(exc),
                max_chars=_max_msg(cfg),
            ),
        ) from exc
    finally:
        if pool is not None:
            # Do not join abandoned workers inside the request thread.
            pool.shutdown(wait=False)
        if spool_path is not None and not timed_out:
            spool_path.unlink(missing_ok=True)


def _run_parse_job(
    task_id: str,
    *,
    unit_id: str,
    file_type: str,
    file_bytes: bytes | None,
    file_path: str | None,
    file_name: str | None,
    markdown: str | None,
    source_url: str | None,
    source_name: str | None,
    chunk_size: int,
    chunk_overlap: int,
    use_vlm: bool,
    cfg: Settings,
    trace_id: str | None = None,
    ingest_id: str | None = None,
) -> None:
    """Background worker body for async parse tasks.

    Rebinds the submitting request's trace_id so worker logs join the same chain.
    """
    if trace_id:
        set_trace_id(trace_id)
    store = get_task_store()
    mineru = get_mineru()
    vlm = get_vlm()
    # These worker-side logs must carry the request trace_id after rebind above.
    logger.info("async parse start task_id=%s unit_id=%s", task_id, unit_id)
    store.update(task_id, status="running", stage="fetch", progress=1, detail="started")

    def on_progress(stage: str, progress: int, detail: str) -> None:
        if store.is_cancelled(task_id):
            raise ParsePipelineError("task cancelled", "TASK_CANCELLED", 499)
        store.update(task_id, stage=stage, progress=progress, detail=detail)

    def should_cancel() -> bool:
        return store.is_cancelled(task_id)

    try:
        result = run_parse(
            unit_id=unit_id,
            file_type=file_type,
            file_bytes=file_bytes,
            file_path=file_path,
            file_name=file_name,
            markdown=markdown,
            source_url=source_url,
            source_name=source_name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            use_vlm=use_vlm,
            cfg=cfg,
            mineru=mineru,
            vlm=vlm,
            progress=on_progress,
            ingest_id=ingest_id,
            should_cancel=should_cancel,
        )
        payload: dict[str, Any] = result.model_dump()
        logger.info("async parse done task_id=%s unit_id=%s", task_id, unit_id)
        store.update(
            task_id,
            status="done",
            stage="done",
            progress=100,
            detail="completed",
            result=payload,
        )
    except ParsePipelineError as exc:
        logger.warning("async parse failed task_id=%s unit_id=%s: %s", task_id, unit_id, exc)
        msg = safe_message(str(exc), cfg.error_message_max_chars)
        store.update(
            task_id,
            status="failed",
            stage="failed",
            detail=msg,
            error_code=exc.error_code,
            error_message=msg,
        )
    except Exception as exc:
        logger.exception("async parse unexpected error task_id=%s", task_id)
        msg = safe_message(str(exc), cfg.error_message_max_chars)
        store.update(
            task_id,
            status="failed",
            stage="failed",
            detail=msg,
            error_code="PARSE_FAILED",
            error_message=msg,
        )
    finally:
        if file_path:
            Path(file_path).unlink(missing_ok=True)
        try:
            _get_parse_sem().release()
        except ValueError:
            logger.warning("parse semaphore over-released task_id=%s", task_id)


@router.post("/parse/async", response_model=ParseTaskAccepted)
async def parse_async(
    file: UploadFile | None = File(default=None),
    markdown: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    source_name: str | None = Form(default=None),
    file_type: str = Form(...),
    unit_id: str = Form(...),
    chunk_size: int | None = Form(default=None),
    chunk_overlap: int | None = Form(default=None),
    use_vlm: bool = Form(default=False),
    ingest_id: str | None = Form(default=None),
    cfg: Settings = Depends(get_settings),
) -> ParseTaskAccepted:
    """Init async parse (debug/compat). Production main path = sync /parse + Java orchestration."""
    ft = _normalize_file_type(file_type)
    uid = _require_unit_id(unit_id)
    size, overlap = _chunk_params(cfg, chunk_size, chunk_overlap)
    _enforce_markdown_limit(markdown, cfg)
    _require_source(file=file, markdown=markdown, source_url=source_url, file_type=ft)
    if ingest_id is not None and len(ingest_id) > 128:
        raise HTTPException(status_code=400, detail="ingest_id max_length=128")

    file_bytes: bytes | None = None
    file_path: str | None = None
    if file is not None:
        file_bytes, spool_path = await _spool_upload(file, cfg)
        if spool_path is not None:
            file_path = str(spool_path)

    sem = _get_parse_sem()
    if not sem.acquire(blocking=False):
        if file_path:
            Path(file_path).unlink(missing_ok=True)
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "PARSE_QUEUE_FULL",
                "message": f"parse queue is full (parse_queue_max={cfg.parse_queue_max})",
            },
        )
    store = get_task_store()
    try:
        task = store.create(unit_id=uid, file_type=ft)
        # Capture request-scoped trace_id before handoff; thread pool does not inherit contextvars.
        submitted_trace_id = get_trace_id()
        _parse_pool.submit(
            _run_parse_job,
            task.task_id,
            unit_id=uid,
            file_type=ft,
            file_bytes=file_bytes,
            file_path=file_path,
            file_name=file.filename if file else None,
            markdown=markdown,
            source_url=source_url,
            source_name=source_name,
            chunk_size=size,
            chunk_overlap=overlap,
            use_vlm=use_vlm,
            cfg=cfg,
            trace_id=submitted_trace_id,
            ingest_id=ingest_id,
        )
    except Exception:
        sem.release()
        if file_path:
            Path(file_path).unlink(missing_ok=True)
        raise
    return ParseTaskAccepted(
        task_id=task.task_id,
        unit_id=uid,
        file_type=ft,
        status="queued",
    )


@router.get("/tasks/{task_id}", response_model=ParseTaskStatus)
def get_task(task_id: str) -> ParseTaskStatus:
    """Poll async parse task status and result."""
    task = get_task_store().get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND", "message": task_id})
    public = task.to_public()
    result = public.get("result")
    return ParseTaskStatus(
        task_id=public["task_id"],
        unit_id=public["unit_id"],
        file_type=public["file_type"],
        status=public["status"],
        stage=public["stage"],
        progress=public["progress"],
        detail=public["detail"],
        error_code=public["error_code"],
        error_message=public["error_message"],
        result=ParseResponse(**result) if result else None,
        created_at=public["created_at"],
        updated_at=public["updated_at"],
    )


@router.delete("/tasks/{task_id}")
def cancel_task(task_id: str) -> dict:
    """Request cancel of queued/running parse task."""
    store = get_task_store()
    if not store.get(task_id):
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND", "message": task_id})
    ok = store.request_cancel(task_id)
    return {"task_id": task_id, "cancel_requested": ok}


@router.post("/embed", response_model=EmbedResponse)
def embed(
    body: EmbedRequest,
    embedder: Embedder = Depends(get_embedder),
    store: VectorStore = Depends(get_vector_store),
    cfg: Settings = Depends(get_settings),
) -> EmbedResponse:
    """Idempotent embed; supports resume_from_index after partial failure.

    Encode completes before old vectors are deleted. Concurrent embeds for the
    same unit_id are serialized via a process-local unit lock.
    """
    from ai.embed.partial import EmbedPartial, get_embed_partial_store

    unit_id = _require_unit_id(body.unit_id)
    max_chunks = int(cfg.embed_max_chunks or 0)
    if max_chunks > 0 and len(body.chunks) > max_chunks:
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "EMBED_TOO_MANY_CHUNKS",
                f"chunks count {len(body.chunks)} exceeds embed_max_chunks={max_chunks}",
                max_chars=_max_msg(cfg),
            ),
        )
    if cfg.embed_require_acl_fields and not (body.acl_department or "").strip():
        get_metrics().inc("retrieve.embed_acl_reject")
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "EMBED_ACL_FIELDS_REQUIRED",
                "acl_department is required when embed_require_acl_fields=true",
                max_chars=_max_msg(cfg),
            ),
        )
    circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
    if circuits.is_open("embed"):
        get_metrics().inc("embed.circuit_open")
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "EMBED_CIRCUIT_OPEN",
                "message": "embed circuit open (cooldown)",
            },
        )

    chunk_dicts = [
        {
            "chunk_index": c.chunk_index,
            "content": c.content,
            "heading_path": c.heading_path,
        }
        for c in body.chunks
    ]
    computed_chunks_hash = chunks_fingerprint(chunk_dicts)

    if body.chunks_hash and body.chunks_hash != computed_chunks_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "CHUNKS_HASH_MISMATCH",
                "message": "request chunks_hash does not match provided chunks",
            },
        )

    empty_count = sum(1 for c in body.chunks if not (c.content or "").strip())
    if empty_count == len(body.chunks):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "EMBED_EMPTY_DOCUMENT",
                "message": "all chunks are empty; refuse embed",
            },
        )
    empty_ratio = empty_count / len(body.chunks)
    if empty_ratio > cfg.embed_max_empty_ratio and not body.force:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "EMBED_QUALITY_GATE",
                "message": (
                    f"empty_chunk_ratio={empty_ratio:.3f} exceeds "
                    f"embed_max_empty_ratio={cfg.embed_max_empty_ratio}; "
                    "pass force=true to override"
                ),
            },
        )

    max_chars = cfg.embed_max_content_chars
    for c in body.chunks:
        text_preview = enrich_chunk_for_embed(c.content, c.heading_path)
        if len(text_preview) > max_chars:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "EMBED_CONTENT_TOO_LONG",
                    "message": (
                        f"chunk_index={c.chunk_index} content length {len(text_preview)} "
                        f"exceeds embed_max_content_chars={max_chars}"
                    ),
                },
            )

    texts = [enrich_chunk_for_embed(c.content, c.heading_path) for c in body.chunks]
    partial_store = get_embed_partial_store(cfg.embed_partial_max)
    resume_from = body.resume_from_index if cfg.embed_resume_enabled else 0
    if resume_from >= len(body.chunks):
        raise HTTPException(status_code=400, detail="resume_from_index out of range")

    try:
        with get_unit_locks().hold(unit_id):
            if resume_from == 0:
                partial_store.clear(unit_id)
                vectors: list[list[float]] = []
            else:
                prev = partial_store.get(unit_id)
                if prev is None or len(prev.vectors) < resume_from:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error_code": "RESUME_CHECKPOINT_MISMATCH",
                            "message": "no partial checkpoint to resume; start from 0",
                        },
                    )
                if prev.chunks_hash and prev.chunks_hash != computed_chunks_hash:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error_code": "RESUME_CHECKPOINT_MISMATCH",
                            "message": "checkpoint chunks_hash does not match current request",
                        },
                    )
                if prev.dim != embedder.dim:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error_code": "RESUME_CHECKPOINT_MISMATCH",
                            "message": f"checkpoint dim={prev.dim} != current dim={embedder.dim}",
                        },
                    )
                current_model = embed_model_id(cfg)
                if prev.embed_model and prev.embed_model != current_model:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error_code": "RESUME_CHECKPOINT_MISMATCH",
                            "message": (
                                f"checkpoint embed_model={prev.embed_model} "
                                f"!= current embed_model={current_model}"
                            ),
                        },
                    )
                vectors = list(prev.vectors[:resume_from])

            batch = max(1, cfg.embed_batch_size)
            embed_policy = RetryPolicy(max_attempts=3, base_delay_sec=0.2, max_delay_sec=5.0)
            for i in range(resume_from, len(texts), batch):
                # Transient embedder faults (5xx/timeout) retry with exponential backoff.
                vectors.extend(
                    retry_call(
                        embedder.encode,
                        texts[i : i + batch],
                        policy=embed_policy,
                    )
                )
                if cfg.embed_resume_enabled:
                    partial_store.put(
                        EmbedPartial(
                            unit_id=unit_id,
                            dim=embedder.dim,
                            vectors=list(vectors),
                            contents=texts[: len(vectors)],
                            chunks_hash=computed_chunks_hash,
                            embed_model=embed_model_id(cfg),
                        )
                    )

            if len(vectors) != len(body.chunks):
                raise RuntimeError(f"encode count mismatch: {len(vectors)} != {len(body.chunks)}")
            items = [
                VectorItem(
                    chunk_index=c.chunk_index,
                    content=texts[i],
                    embedding=vectors[i],
                    embed_model=embed_model_id(cfg),
                    ingest_id=body.ingest_id or "",
                    acl_department=body.acl_department or "",
                    acl_classification=body.acl_classification or "",
                    heading_path=list(c.heading_path or []),
                )
                for i, c in enumerate(body.chunks)
            ]
            # Atomic version publish: temp write → validate → switch → cleanup.
            count = store.replace_unit(unit_id, items)
            partial_store.clear(unit_id)
            circuits.record_success("embed")
            get_metrics().inc("embed.ok")
            # Keep process-local lexical index aligned with dense path (same chunk ids).
            try:
                version_id = store.get_active_version(unit_id) or "legacy"
                get_lexical_index().replace_unit(
                    unit_id,
                    version_id,
                    [
                        {
                            "id": _stable_id(unit_id, item.chunk_index, version_id if version_id != "legacy" else ""),
                            "chunk_index": item.chunk_index,
                            "content": item.content,
                            "version_id": version_id,
                            "acl_department": item.acl_department,
                            "acl_classification": item.acl_classification,
                            "heading_path": item.heading_path or [],
                        }
                        for item in items
                    ],
                )
            except Exception:
                logger.warning("lexical index update failed unit_id=%s", unit_id, exc_info=True)
    except HTTPException:
        get_metrics().inc("embed.quality_or_validation_fail")
        raise
    except PublishValidationError as exc:
        logger.exception("embed publish failed unit_id=%s resume_from=%s", unit_id, resume_from)
        get_metrics().inc("embed.publish_fail")
        # Input-shaped validation (length/model mix) → 400; store faults stay 500.
        status = 400 if "exceeds" in str(exc) or "mixed embed_model" in str(exc) else 500
        raise HTTPException(
            status_code=status,
            detail=detail_body(
                exc.error_code,
                str(exc),
                max_chars=_max_msg(cfg),
                resume_hint=f"retry with resume_from_index={resume_from}",
            ),
        ) from exc
    except Exception as exc:
        logger.exception("embed failed unit_id=%s resume_from=%s", unit_id, resume_from)
        circuits.record_failure("embed", str(exc))
        get_metrics().inc("embed.fail")
        raise HTTPException(
            status_code=500,
            detail=detail_body(
                "EMBED_FAILED",
                str(exc),
                max_chars=_max_msg(cfg),
                resume_hint=f"retry with resume_from_index={resume_from}",
            ),
        ) from exc

    return EmbedResponse(
        unit_id=unit_id,
        embedded_count=count,
        dim=embedder.dim,
        resumed_from=resume_from,
        partial=False,
        ingest_id=body.ingest_id or None,
    )


@router.delete("/units/{unit_id}", response_model=DeleteUnitResponse)
def delete_unit(
    unit_id: str,
    store: VectorStore = Depends(get_vector_store),
) -> DeleteUnitResponse:
    """Delete vectors for unit; idempotent. Never fake success on failure."""
    uid = _require_unit_id(unit_id)
    try:
        with get_unit_locks().hold(uid):
            deleted_count = store.delete_by_unit(uid)
            try:
                get_lexical_index().delete_unit(uid)
            except Exception:
                logger.warning("lexical index delete failed unit_id=%s", uid, exc_info=True)
    except Exception as exc:
        logger.exception("delete unit failed unit_id=%s", uid)
        raise HTTPException(
            status_code=500,
            detail=detail_body(
                "DELETE_FAILED",
                str(exc),
                max_chars=_max_msg(get_settings()),
            ),
        ) from exc
    return DeleteUnitResponse(deleted=True, deleted_count=deleted_count)

