"""Cloud LLM client for /internal/generate (OpenAI-compatible chat)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx

from core.config import Settings
from schemas.ingest import GenerateContext

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是售后知识库助手。只依据提供的资料回答；资料不足时明确说不知道，禁止编造保修、价格、SLA。"
    "用简体中文，简洁准确。"
)


class LlmError(RuntimeError):
    def __init__(self, message: str, error_code: str = "LLM_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


class LlmClient:
    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    @property
    def available(self) -> bool:
        return bool(self._cfg.llm_api_base and self._cfg.llm_api_key and self._cfg.llm_model)

    def stream_answer(self, question: str, contexts: list[GenerateContext]) -> Iterator[str]:
        """Yield answer text deltas. Does not re-retrieve; only uses given contexts."""
        if not self.available:
            raise LlmError("LLM not configured", "LLM_DISABLED")

        lines = []
        if not contexts:
            lines.append("（无可用资料）")
        else:
            for i, c in enumerate(contexts, 1):
                title = c.title or f"资料{i}"
                lines.append(f"【{title}】(unit_id={c.unit_id})\n{c.content}")
        materials = "\n\n".join(lines)
        user_prompt = f"资料：\n{materials}\n\n问题：{question}\n\n请仅依据资料回答。"

        payload = {
            "model": self._cfg.llm_model,
            "temperature": self._cfg.llm_temperature,
            "stream": True,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        url = self._cfg.llm_api_base.rstrip("/") + "/chat/completions"
        try:
            with httpx.stream(
                "POST",
                url,
                headers={"Authorization": f"Bearer {self._cfg.llm_api_key}"},
                json=payload,
                timeout=self._cfg.llm_timeout_sec,
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="replace")[:200]
                    raise LlmError(f"LLM HTTP {resp.status_code}: {body}", "LLM_REQUEST_FAILED")
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta
        except LlmError:
            raise
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM stream failed: {exc}", "LLM_REQUEST_FAILED") from exc
