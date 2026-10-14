"""Session memory routes (/internal/session/*).

``/session/append`` is used for turns that do NOT go through ``/internal/generate``
(FAQ hit / clarify ask / clarify reference). Chosen-product turns and normal QA turns
are persisted by ``/internal/generate`` itself, so they need no extra round-trip.

Best-effort by design: a Mongo failure must never fail the caller's main path, so this
route always returns 200 and reports ``ok=false`` when nothing was written.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from ai.session.store import SessionTurn, get_session_store
from schemas.session import SessionAppendRequest, SessionAppendResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-session"])


@router.post("/session/append", response_model=SessionAppendResponse)
def append_session_turn(body: SessionAppendRequest) -> SessionAppendResponse:
    """Append one turn to the session document (idempotent on ``turn_id``)."""
    store = get_session_store()
    turn = SessionTurn(
        turn_id=body.turn_id,
        type=body.turn_type or "qa",
        question=body.question or "",
        answer=body.answer or "",
        authorized_unit_ids=list(body.authorized_unit_ids or []),
        referenced_unit_ids=list(body.referenced_unit_ids or []),
        chosen_unit_id=body.chosen_unit_id,
        faq_hit=bool(body.faq_hit),
    )
    seq = store.append_turn(owner_id=body.owner_id, session_id=body.session_id, turn=turn)
    return SessionAppendResponse(ok=seq is not None, seq=seq)
