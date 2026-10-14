"""Session memory DTOs (/internal/session/*)."""

from pydantic import BaseModel, Field


class SessionAppendRequest(BaseModel):
    """One turn to persist. All ids are opaque to the AI side."""

    session_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    turn_type: str = "qa"  # qa | faq | clarify_ask | clarify_reference | chosen
    question: str = ""
    answer: str = ""
    authorized_unit_ids: list[str] = Field(default_factory=list)
    referenced_unit_ids: list[str] = Field(default_factory=list)
    chosen_unit_id: str | None = None
    faq_hit: bool = False


class SessionAppendResponse(BaseModel):
    """Append result. ``ok=false`` (with ``seq=null``) means no-op / degraded."""

    ok: bool
    seq: int | None = None
