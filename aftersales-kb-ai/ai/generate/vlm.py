"""Cloud VLM client: image bytes → Chinese caption (OpenAI-compatible chat vision)."""

from __future__ import annotations

import base64
import logging

import httpx

from ai.media_refs import image_ref_keys
from core.config import Settings

logger = logging.getLogger(__name__)

_PROMPT = (
    "你是售后知识库助手。用简体中文描述这张图中与设备/接线/指示灯/面板/故障相关的关键信息，"
    "2-4 句，客观具体，不要寒暄。若是装饰图/无信息图，只写「无有效图示信息」。"
)

# Ingest review gate (阶段1): 独立常量，勿改上面的 _PROMPT（有 sha256 快照断言）。
_REVIEW_VLM_PROMPT = (
    "你是入库质检员。判断这张售后手册配图是否清晰、与设备/接线/指示灯/面板/故障相关且信息可用。"
    '只输出 JSON：{"usable": true|false, "reason": "..."}。'
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
            raise VlmError(
                f"VLM unexpected response: {str(data)[:200]}", "VLM_BAD_RESPONSE"
            ) from exc

        if isinstance(text, list):
            # some APIs return content parts
            text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
        text = (text or "").strip()
        if not text:
            raise VlmError("VLM returned empty caption", "VLM_EMPTY_CAPTION")
        return text

    def assess_image(
        self, image_bytes: bytes, *, mime: str = "image/jpeg", timeout: float | None = None
    ) -> tuple[bool, str]:
        """入库质检抽检：判断一张配图是否清晰、可用。

        返回 ``(usable, reason)``。请求/解析失败一律抛 ``VlmError``，由调用方
        决定 fail-closed（转人工）。``timeout`` 缺省沿用 ``vlm_timeout_sec``
        （review 档会传入更小的 ``review_vlm_timeout_sec``）。使用独立的
        ``_REVIEW_VLM_PROMPT``，不改动 ``describe_image`` 的行为与 ``_PROMPT``。
        """
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
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _REVIEW_VLM_PROMPT},
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
                timeout=timeout if timeout is not None else self._cfg.vlm_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise VlmError(f"VLM request failed: {exc}", "VLM_REQUEST_FAILED") from exc

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VlmError(
                f"VLM unexpected response: {str(data)[:200]}", "VLM_BAD_RESPONSE"
            ) from exc

        if isinstance(text, list):
            text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
        text = (text or "").strip()
        if not text:
            raise VlmError("VLM returned empty verdict", "VLM_EMPTY_CAPTION")

        obj = _parse_review_verdict(text)
        if obj is None:
            raise VlmError(f"VLM verdict not parseable: {text[:200]}", "VLM_BAD_RESPONSE")
        return bool(obj.get("usable")), str(obj.get("reason") or "")


def _parse_review_verdict(raw: str) -> dict | None:
    """Tolerant JSON extraction for the VLM review verdict (``{"usable": bool, ...}``)."""
    import json
    import re

    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "usable" not in obj:
        return None
    return obj


def inject_captions_into_markdown(
    markdown: str,
    captions_by_path: dict[str, str],
) -> str:
    """Insert blockquote captions after image links that have a caption.

    Matching is exact-first on the normalized path, then basename fallback, so a
    caption keyed by ``extract_x/images/a.jpg`` still injects into a markdown
    link written ``images/a.jpg`` / ``./images/a.jpg`` / ``images\\a.jpg``.
    """
    if not markdown or not captions_by_path:
        return markdown

    import re

    # exact: normalized full path -> caption; by_base: basename -> caption.
    # Basename collisions keep the first key seen (do not overwrite).
    exact: dict[str, str] = {}
    by_base: dict[str, str] = {}
    for key, value in captions_by_path.items():
        if not value:
            continue
        keys = image_ref_keys(key)
        if not keys:
            continue
        if keys[0] not in exact:
            exact[keys[0]] = value
        if len(keys) > 1 and keys[1] not in by_base:
            by_base[keys[1]] = value

    def repl(m: re.Match) -> str:
        keys = image_ref_keys(m.group(2))
        caption = exact.get(keys[0]) if keys else None
        if not caption and len(keys) > 1:
            caption = by_base.get(keys[1])
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
