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
    # "kb"（默认，严格依据资料）或 "chat"（闲聊/通用问答兜底，使用 _CHAT_SYSTEM_PROMPT）。
    # 默认值保证向后兼容：老调用方不带该字段时行为字节不变。
    mode: str = "kb"
    # 会话记忆：owner_id 为 Java 下发的不透明分区键；acl_unit_ids 为当前可访问单元编码。
    owner_id: str = ""
    turn_id: str = ""
    acl_unit_ids: list[str] = Field(default_factory=list)
    chosen_unit_id: int | None = None
    write_history: bool = True
