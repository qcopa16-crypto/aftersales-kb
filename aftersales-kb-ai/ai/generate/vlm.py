"""Cloud VLM client: image bytes → Chinese caption (OpenAI-compatible chat vision)."""

from __future__ import annotations

import base64
import logging

import httpx

from core.config import Settings

logger = logging.getLogger(__name__)

_PROMPT = (
    "你是售后知识库助手。用简体中文描述这张图中与设备/接线/指示灯/面板/故障相关的关键信息，"
    "2-4 句，客观具体，不要寒暄。若是装饰图/无信息图，只写「无有效图示信息」。"
)


class VlmError(RuntimeError):
    def __init__(self, message: str, error_code: str = "VLM_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


class VlmClient:
    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg

    @property
    def available(self) -> bool:
        return bool(
            self._cfg.vlm_enabled
            and self._cfg.vlm_api_base
            and self._cfg.vlm_api_key
            and self._cfg.vlm_model
        )

    def describe_image(self, image_bytes: bytes, *, mime: str = "image/jpeg") -> str:
        if not self.available:
            raise VlmError("VLM not configured or disabled", "VLM_DISABLED")
        if not image_bytes:
            raise VlmError("empty image", "VLM_EMPTY_IMAGE")
        if len(image_bytes) > self._cfg.vlm_max_image_bytes:
            raise VlmError(
                f"image too large: {len(image_bytes)} > {self._cfg.vlm_max_image_bytes}",
                "VLM_IMAGE_TOO_LARGE",
            )

        b64 = base64.b64encode(image_bytes).decode("ascii")
        url = self._cfg.vlm_api_base.rstrip("/") + "/chat/completions"
        payload = {
            "model": self._cfg.vlm_model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
        }
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {self._cfg.vlm_api_key}"},
                json=payload,
                timeout=self._cfg.vlm_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise VlmError(f"VLM request failed: {exc}", "VLM_REQUEST_FAILED") from exc

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VlmError(f"VLM unexpected response: {str(data)[:200]}", "VLM_BAD_RESPONSE") from exc

        if isinstance(text, list):
            # some APIs return content parts
            text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
        text = (text or "").strip()
        if not text:
            raise VlmError("VLM returned empty caption", "VLM_EMPTY_CAPTION")
        return text


def inject_captions_into_markdown(
    markdown: str,
    captions_by_path: dict[str, str],
) -> str:
    """Insert blockquote captions after image links that have a caption."""
    if not markdown or not captions_by_path:
        return markdown

    import re

    def repl(m: re.Match) -> str:
        alt, path = m.group(1), m.group(2).strip()
        caption = captions_by_path.get(path)
        block = m.group(0)
        if not caption:
            return block
        return f"{block}\n\n> 【图示】{caption}"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, markdown)


def guess_mime(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".bmp"):
        return "image/bmp"
    return "image/jpeg"
