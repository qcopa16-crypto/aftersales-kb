"""Generate /internal contract DTOs."""
from pydantic import BaseModel, Field


class GenerateContext(BaseModel):
    unit_id: str
    title: str = ""
    content: str = Field(min_length=1)


class GenerateRequest(BaseModel):
    question: str = Field(min_length=1)
    contexts: list[GenerateContext] = Field(default_factory=list)
    session_id: str = ""
