"""VLM /internal routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from core.config import Settings, get_settings
from app.deps import get_vlm
from schemas.ingest import VlmDescribeResponse
from ai.generate.vlm import VlmClient, VlmError, guess_mime
from core.errors import detail_body
from core.limits import get_limits

from app.api.internal._common import _max_msg

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-vlm"])

@router.post("/vlm/describe", response_model=VlmDescribeResponse)
async def vlm_describe(
    file: UploadFile = File(...),
    cfg: Settings = Depends(get_settings),
    vlm: VlmClient = Depends(get_vlm),
) -> VlmDescribeResponse:
    """Describe one image with cloud VLM (optional capability)."""
    if not vlm.available:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "VLM_DISABLED", "message": "VLM disabled or not configured"},
        )
    raw = await file.read()
    mime = file.content_type or guess_mime(file.filename or "")
    if not mime.startswith("image/"):
        mime = guess_mime(file.filename or "")
    try:
        with get_limits().vlm_slot():
            caption = vlm.describe_image(raw, mime=mime)
    except VlmError as exc:
        status = 413 if exc.error_code == "VLM_IMAGE_TOO_LARGE" else 502
        raise HTTPException(
            status_code=status,
            detail=detail_body(
                exc.error_code,
                str(exc),
                max_chars=_max_msg(cfg),
            ),
        ) from exc
    return VlmDescribeResponse(caption=caption, model=cfg.vlm_model)

