"""Generate /internal routes (SSE). Never re-retrieves."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.config import get_settings
from app.deps import get_llm
from schemas.generate import GenerateContext, GenerateRequest
from ai.generate.llm import LlmClient, LlmError
from core.errors import detail_body, safe_message, sanitize_text
from core.limits import get_limits

from app.api.internal._common import _max_msg

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-generate"])

@router.post("/generate")
def generate(
    body: GenerateRequest,
    llm: LlmClient = Depends(get_llm),
):
    """SSE stream from authorized contexts only; never re-retrieves."""
    from fastapi.responses import StreamingResponse

    import json as _json
    import time as _time

    cfg = get_settings()
    # Length + control-char hygiene only; full prompt-injection detection is out of scope.
    question = sanitize_text(body.question)
    if not question:
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "PAYLOAD_TOO_LARGE",
                "question is empty after sanitization",
                max_chars=_max_msg(cfg),
            ),
        )
    q_limit = int(cfg.generate_max_question_chars or 0)
    if q_limit > 0 and len(question) > q_limit:
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "PAYLOAD_TOO_LARGE",
                f"question length {len(question)} exceeds "
                f"generate_max_question_chars={q_limit}",
                max_chars=_max_msg(cfg),
            ),
        )
    total_ctx = sum(len(c.content or "") for c in body.contexts)
    ctx_limit = int(cfg.generate_max_contexts_total_chars or 0)
    if ctx_limit > 0 and total_ctx > ctx_limit:
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "PAYLOAD_TOO_LARGE",
                f"contexts total content length {total_ctx} exceeds "
                f"generate_max_contexts_total_chars={ctx_limit}",
                max_chars=_max_msg(cfg),
            ),
        )
    # Rebuild contexts with control chars stripped (keep unit_id/title shape).
    clean_contexts = [
        GenerateContext(
            unit_id=c.unit_id,
            title=sanitize_text(c.title or ""),
            content=sanitize_text(c.content or ""),
        )
        for c in body.contexts
    ]
    clean_contexts = [c for c in clean_contexts if c.content]
    body = GenerateRequest(
        question=question,
        contexts=clean_contexts,
        session_id=body.session_id,
    )

    if not llm.available:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "LLM_DISABLED", "message": "LLM not configured"},
        )

    started = _time.monotonic()

    def sse_gen():
        n_chars = 0
        try:
            with get_limits().llm_slot():
                for delta in llm.stream_answer(body.question, body.contexts):
                    n_chars += len(delta)
                    yield f"event: delta\ndata: {_json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            # token counts unavailable without usage in stream; expose elapsed only
            done = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "elapsed_ms": elapsed_ms,
                "faqHit": False,
                "session_id": body.session_id,
            }
            yield f"event: done\ndata: {_json.dumps(done, ensure_ascii=False)}\n\n"
        except LlmError as exc:
            err = {"code": exc.error_code, "message": safe_message(str(exc), _max_msg(cfg))}
            yield f"event: error\ndata: {_json.dumps(err, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("generate stream failed session_id=%s", body.session_id)
            err = {"code": "GENERATE_FAILED", "message": safe_message(str(exc), _max_msg(cfg))}
            yield f"event: error\ndata: {_json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
