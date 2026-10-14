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
    "回答中每条结论后用 [n] 标注所依据的资料编号（可引用多条，如 [1][2]）。"
    "用简体中文，简洁准确。"
)

# Phase A2.1 (opt-in via `generate_citation_strict`): tighten citation precision
# without touching the default prompt. See docs/retrieval-gate-decision.md.
_CITATION_STRICT_SUFFIX = (
    "引用要精确：只标注**直接支撑该条结论**的资料编号；与结论无直接关系的资料不要引用，"
    "不确定是否相关时宁可不引用。"
)

# 闲聊/通用问答兜底（mode="chat"）专用 system prompt。
# 必须是**独立常量**：`tests/test_generate_prompt_citation.py` 对 `_SYSTEM_PROMPT`
# 有 sha256 快照断言，改默认 prompt 会直接挡红。
_CHAT_SYSTEM_PROMPT = (
    "你是售后知识库助手，现在用你自己的通用知识回答。这是通用知识，不是公司规定。"
    "凡涉及本公司的保修期限、价格、SLA、赔偿、政策条款等具体规定，一律回答"
    "「这需要查公司资料，我无法给出准确答复」，不得给出任何具体数字或承诺。"
    "可以回答通用技术常识、故障排查思路、概念解释、系统使用帮助与寒暄。"
    "不确定就说不确定，禁止编造。用简体中文，简洁准确。"
)


def system_prompt(cfg: Settings | None = None) -> str:
    """The system prompt; unchanged unless `generate_citation_strict` is set."""
    if cfg is not None and getattr(cfg, "generate_citation_strict", False):
        return _SYSTEM_PROMPT + _CITATION_STRICT_SUFFIX
    return _SYSTEM_PROMPT


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

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 200,
        timeout: float | None = None,
        disable_thinking: bool = False,
    ) -> str:
        """Non-streaming completion (used by query rewrite and similar helpers).

        `disable_thinking=True` asks reasoning models (e.g. GLM-4.5) to skip the
        hidden reasoning pass. Without it a small `max_tokens` is fully consumed
        by `reasoning_content`, `finish_reason` becomes "length" and `content`
        comes back empty — which silently breaks short-output helpers.
        """
        if not self.available:
            raise LlmError("LLM not configured", "LLM_DISABLED")
        payload = {
            "model": self._cfg.llm_model,
            "temperature": self._cfg.llm_temperature,
            "stream": False,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        url = self._cfg.llm_api_base.rstrip("/") + "/chat/completions"
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {self._cfg.llm_api_key}"},
                json=payload,
                timeout=timeout or self._cfg.llm_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content", "") or ""
            if not content and choice.get("finish_reason") == "length":
                # Reasoning-only response (budget exhausted before the answer).
                logger.warning(
                    "LLM complete returned empty content (finish_reason=length); "
                    "raise max_tokens or pass disable_thinking=True"
                )
            return content
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM complete failed: {exc}", "LLM_REQUEST_FAILED") from exc

    def stream_answer(
        self,
        question: str,
        contexts: list[GenerateContext],
        usage: dict[str, int] | None = None,
        mode: str = "kb",
        history: str = "",
    ) -> Iterator[str]:
        """Yield answer text deltas. Does not re-retrieve; only uses given contexts.

        When `usage` is provided it is filled with the provider-reported token
        counts (``prompt_tokens`` / ``completion_tokens``). This requires
        ``stream_options.include_usage`` — see ``llm_stream_include_usage``.

        ``mode="kb"`` (default) answers strictly from the given contexts.
        ``mode="chat"`` answers from the model's own general knowledge using
        ``_CHAT_SYSTEM_PROMPT``; the user prompt is the raw question (no
        "资料：" prefix and no "（无可用资料）" placeholder).

        ``history`` (kb mode only) is an ALREADY ACL-filtered plain-text block of
        recent turns, injected before the materials **for pronoun resolution only**.
        It is explicitly labelled as non-citable, and ``[n]`` markers are stripped
        upstream so history cannot pollute this turn's citation numbering. Empty
        history leaves the prompt byte-identical to before.
        """
        if not self.available:
            raise LlmError("LLM not configured", "LLM_DISABLED")

        if mode == "chat":
            system_content = _CHAT_SYSTEM_PROMPT
            user_prompt = question
        else:
            lines = []
            if not contexts:
                lines.append("（无可用资料）")
            else:
                for i, c in enumerate(contexts, 1):
                    title = c.title or f"资料{i}"
                    lines.append(f"【{i}】{title}\n{c.content}")
            materials = "\n\n".join(lines)
            history_block = ""
            if history and history.strip():
                history_block = (
                    "对话历史（仅供理解指代，不作为回答依据；历史回答的引用编号已省略）：\n"
                    f"{history}\n\n"
                )
            user_prompt = (
                f"{history_block}资料：\n{materials}\n\n问题：{question}\n\n"
                "请仅依据资料回答，并在每条结论后用 [n] 标注来源编号；资料不足时说明不知道。"
            )
            system_content = system_prompt(self._cfg)

        payload = {
            "model": self._cfg.llm_model,
            "temperature": self._cfg.llm_temperature,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_prompt},
            ],
        }
        if getattr(self._cfg, "llm_disable_thinking", False):
            payload["thinking"] = {"type": "disabled"}
        if getattr(self._cfg, "llm_stream_include_usage", True):
            payload["stream_options"] = {"include_usage": True}
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
                    # usage 出现在最后一个 chunk（此时 choices 为空数组），
                    # 必须先于 choices 判断处理，否则会被 continue 丢掉。
                    chunk_usage = chunk.get("usage")
                    if chunk_usage and usage is not None:
                        usage["prompt_tokens"] = int(chunk_usage.get("prompt_tokens") or 0)
                        usage["completion_tokens"] = int(chunk_usage.get("completion_tokens") or 0)
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
