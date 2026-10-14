"""Generate /internal routes (SSE). Never re-retrieves."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.config import get_settings
from app.deps import get_llm
from schemas.generate import GenerateContext, GenerateRequest
from ai.generate.llm import LlmClient, LlmError
from ai.session.history import filter_turns, render_history
from ai.session.store import SessionTurn, get_session_store
from core.errors import detail_body, safe_message, sanitize_text
from core.limits import get_limits

from app.api.internal._common import _max_msg

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-generate"])


def _write_turn_best_effort(cfg, body: GenerateRequest, answer_text: str, usage: dict) -> None:
    """Persist this turn to session memory (best-effort; never raises).

    Written by the AI side (the only Mongo holder). A failure is logged and ignored
    so it can never break the SSE stream the user already received.
    """
    if not (getattr(cfg, "session_memory_enabled", False) and body.write_history):
        return
    if not body.owner_id or not body.session_id:
        return
    try:
        import uuid

        if body.chosen_unit_id is not None:
            turn_type = "chosen"
        elif body.mode == "chat":
            turn_type = "llm_general"
        else:
            turn_type = "qa"
        turn = SessionTurn(
            turn_id=body.turn_id or uuid.uuid4().hex,
            type=turn_type,
            question=body.question,
            answer=answer_text,
            # 只记本轮**实际使用**的单元（而非整个 acl 集合），历史过滤更精确
            authorized_unit_ids=[c.unit_id for c in body.contexts],
            referenced_unit_ids=[],
            chosen_unit_id=str(body.chosen_unit_id) if body.chosen_unit_id is not None else None,
            faq_hit=False,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        get_session_store().append_turn(
            owner_id=body.owner_id, session_id=body.session_id, turn=turn
        )
    except Exception:
        logger.warning("session turn write failed (ignored)", exc_info=True)


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
                f"question length {len(question)} exceeds generate_max_question_chars={q_limit}",
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
    # 闲聊/通用问答兜底：AI 侧的权威闸门（opt-in）。关闭时，即使上游下发
    # mode="chat" 也一律按 "kb" 处理（与 Java 侧 ai.chat-fallback-enabled 双保险）。
    mode = (
        "chat"
        if (body.mode == "chat" and getattr(cfg, "generate_chat_fallback_enabled", False))
        else "kb"
    )
    # Phase A2.2 (opt-in): pack contexts (dedup near-duplicates, per-chunk cap,
    # budget, optional item cap) before the prompt. Default off => verbatim.
    # chat 模式没有资料，直接跳过 pack。
    if mode != "chat" and getattr(cfg, "generate_pack_enabled", False):
        from ai.generate.context_pack import pack_contexts

        packed = pack_contexts(
            [
                {"unit_id": c.unit_id, "title": c.title, "content": c.content}
                for c in clean_contexts
            ],
            max_chars=int(cfg.generate_pack_max_chars or 6000),
            per_chunk_max=int(cfg.generate_pack_per_chunk_max or 1500),
            jaccard_threshold=float(cfg.generate_pack_jaccard or 0.85),
            max_items=int(getattr(cfg, "generate_pack_max_items", 0) or 0),
        )
        clean_contexts = [
            GenerateContext(unit_id=p.unit_id, title=p.title, content=p.content) for p in packed
        ]
    # 会话记忆：读 Mongo -> ACL 过滤 -> 才允许用于 prompt（顺序不能颠倒）。
    # 读失败 -> 空历史（绝不"读失败 -> 不过滤"）；AI 只执行 Java 下发的 acl_unit_ids。
    history_block = ""
    if getattr(cfg, "session_memory_enabled", False) and body.owner_id and body.session_id:
        try:
            turns = get_session_store().get_turns(
                owner_id=body.owner_id, session_id=body.session_id, limit=20
            )
            safe_turns = filter_turns(turns, list(body.acl_unit_ids or []))
            history_block = render_history(
                safe_turns,
                max_turns=int(getattr(cfg, "session_history_max_turns", 4) or 4),
                max_chars=int(getattr(cfg, "session_history_max_chars", 2000) or 2000),
            )
        except Exception:
            logger.warning("session history read failed; continue without history", exc_info=True)
            history_block = ""
    # 重建请求时必须带上**所有**新字段，否则被 pydantic 静默丢弃（mode 同理）
    body = GenerateRequest(
        question=question,
        contexts=clean_contexts,
        session_id=body.session_id,
        mode=mode,
        owner_id=body.owner_id,
        turn_id=body.turn_id,
        acl_unit_ids=list(body.acl_unit_ids or []),
        chosen_unit_id=body.chosen_unit_id,
        write_history=body.write_history,
    )

    if not llm.available:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "LLM_DISABLED", "message": "LLM not configured"},
        )

    started = _time.monotonic()

    def sse_gen():
        n_chars = 0
        parts: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        try:
            with get_limits().llm_slot():
                for delta in llm.stream_answer(
                    body.question, body.contexts, usage, mode=body.mode, history=history_block
                ):
                    n_chars += len(delta)
                    parts.append(delta)
                    yield f"event: delta\ndata: {_json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            # token 数来自 provider 的 stream_options.include_usage 回传；
            # 若 provider 不支持（llm_stream_include_usage=false）则保持 0。
            done = {
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "elapsed_ms": elapsed_ms,
                "faqHit": False,
                "session_id": body.session_id,
            }
            yield f"event: done\ndata: {_json.dumps(done, ensure_ascii=False)}\n\n"
            # 收尾 best-effort 写会话记忆（失败不影响已流式输出的答案）
            _write_turn_best_effort(cfg, body, "".join(parts), usage)
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
