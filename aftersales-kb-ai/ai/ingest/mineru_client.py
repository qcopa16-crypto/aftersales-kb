"""MinerU cloud API client: batch upload PDF → poll → download zip markdown."""

from __future__ import annotations

import hashlib
import io
import logging
import time
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from core.config import Settings, get_settings
from ai.ingest.mineru_batch_store import get_mineru_batch_store
from core.circuit import get_circuits, get_metrics
from core.limits import get_limits
from core.retry import RetryPolicy, retry_call

logger = logging.getLogger(__name__)

# extract-result states
_DONE = {"done", "success", "completed"}
_FAILED = {"failed", "error", "cancelled"}

# (stage, progress_0_100, detail) — used by async worker / Java-visible logs.
ProgressFn = Callable[[str, int, str], None]
CancelFn = Callable[[], bool]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class MinerUError(RuntimeError):
    def __init__(self, message: str, error_code: str = "MINERU_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


class MinerUClient:
    """Implements mineru.net 精准解析 API (batch local-file upload)."""

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg
        self._base = cfg.mineru_api_base.rstrip("/")
        self._token = cfg.mineru_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def pdf_to_markdown(
        self,
        pdf_path: Path,
        *,
        on_progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
    ) -> tuple[str, int | None, list[dict]]:
        """Return (markdown, page_count, images_meta). Images listed but not VLM-described."""
        cfg_live = get_settings()
        circuits = get_circuits(cfg_live.circuit_failure_threshold, cfg_live.circuit_cooldown_sec)
        metrics = get_metrics()
        if circuits.is_open("mineru"):
            metrics.inc("mineru.circuit_open")
            raise MinerUError("MinerU circuit open (cooldown)", "MINERU_CIRCUIT_OPEN")
        if should_cancel and should_cancel():
            raise MinerUError("task cancelled before MinerU submit", "TASK_CANCELLED")

        if not self._cfg.mineru_enabled:
            raise MinerUError("MinerU disabled by MINERU_ENABLED=false", "MINERU_DISABLED")
        if not self._token:
            raise MinerUError("MINERU_TOKEN is empty", "MINERU_NO_TOKEN")
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            raise MinerUError(f"pdf not found or empty: {pdf_path}", "MINERU_BAD_FILE")

        limits = get_limits()
        batch_store = get_mineru_batch_store(cfg_live.mineru_batch_store_path)
        file_hash = _file_sha256(pdf_path)
        existing_batch = batch_store.get_batch_id(file_hash)

        t0 = time.monotonic()
        try:
            if existing_batch:
                # Resume poll on a previously submitted batch; do not re-consume daily quota.
                logger.info("MinerU resume batch_id=%s file=%s", existing_batch, pdf_path.name)
                metrics.inc("mineru.resume")
                try:
                    result = self._poll_result(
                        existing_batch, on_progress=on_progress, should_cancel=should_cancel
                    )
                    out = self._parse_result(result)
                    batch_store.clear(file_hash)
                    circuits.record_success("mineru")
                    metrics.inc("mineru.ok")
                    metrics.observe_ms("mineru", (time.monotonic() - t0) * 1000)
                    return out
                except MinerUError as exc:
                    if exc.error_code in {"MINERU_TIMEOUT", "TASK_CANCELLED"}:
                        # Keep batch_id for the next attempt (cancel/timeout are not terminal).
                        raise
                    batch_store.clear(file_hash)
                    raise

            if not limits.try_consume_mineru_file(1):
                raise MinerUError(
                    f"MinerU daily file quota exceeded ({limits.mineru_daily_file_limit})",
                    "MINERU_DAILY_QUOTA",
                )

            # submit: process concurrency + 50 files/min vendor limit
            try:
                with limits.mineru_submit_slot(timeout=90.0):
                    batch_id, upload_url = retry_call(
                        self._create_upload,
                        pdf_path.name,
                        policy=RetryPolicy(max_attempts=3, base_delay_sec=0.5, max_delay_sec=8.0),
                    )
                    retry_call(
                        self._upload_file,
                        upload_url,
                        pdf_path,
                        policy=RetryPolicy(max_attempts=2, base_delay_sec=0.5, max_delay_sec=4.0),
                    )
            except TimeoutError as exc:
                raise MinerUError("MinerU submit rate limit timeout", "MINERU_RATE_LIMIT") from exc

            batch_store.put(file_hash, batch_id, file_name=pdf_path.name)
            result = self._poll_result(batch_id, on_progress=on_progress, should_cancel=should_cancel)
            out = self._parse_result(result)
            batch_store.clear(file_hash)
            circuits.record_success("mineru")
            metrics.inc("mineru.ok")
            metrics.observe_ms("mineru", (time.monotonic() - t0) * 1000)
            return out
        except MinerUError as exc:
            # Cancel/timeout are not dependency health signals; do not trip circuit.
            if exc.error_code not in {
                "MINERU_DAILY_QUOTA",
                "TASK_CANCELLED",
                "MINERU_TIMEOUT",
                "MINERU_CIRCUIT_OPEN",
            }:
                circuits.record_failure("mineru", str(exc))
            if exc.error_code != "TASK_CANCELLED":
                metrics.inc("mineru.fail")
            raise

    def _create_upload(self, file_name: str) -> tuple[str, str]:
        payload = {
            "files": [
                {
                    "name": file_name,
                    "is_ocr": self._cfg.mineru_is_ocr,
                }
            ],
            "enable_table": self._cfg.mineru_enable_table,
            "enable_formula": self._cfg.mineru_enable_formula,
            "model_version": self._cfg.mineru_model_version,
            "language": self._cfg.mineru_language,
        }
        try:
            resp = httpx.post(
                f"{self._base}/api/v4/file-urls/batch",
                headers=self._headers(),
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise MinerUError(f"MinerU upload-url request failed: {exc}", "MINERU_UPLOAD_URL_FAILED") from exc

        if data.get("code") not in (0, None) and data.get("code") != 0:
            # some gateways omit code on success; only reject explicit non-zero
            if data.get("code") != 0:
                raise MinerUError(f"MinerU apply url failed: {data.get('msg') or data}", "MINERU_BAD_RESPONSE")

        body = data.get("data") or {}
        batch_id = body.get("batch_id")
        urls = body.get("file_urls") or body.get("upload_urls") or []
        if not batch_id or not urls:
            raise MinerUError(f"MinerU unexpected response: {data}", "MINERU_BAD_RESPONSE")
        return batch_id, urls[0]

    def _upload_file(self, upload_url: str, pdf_path: Path) -> None:
        try:
            with pdf_path.open("rb") as f:
                # docs: PUT raw body, no Content-Type
                resp = httpx.put(upload_url, content=f.read(), timeout=300.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MinerUError(f"MinerU file upload failed: {exc}", "MINERU_UPLOAD_FAILED") from exc

    def _poll_result(
        self,
        batch_id: str,
        *,
        on_progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
    ) -> dict:
        deadline = time.monotonic() + self._cfg.mineru_poll_timeout_sec
        interval = max(0.5, self._cfg.mineru_poll_interval_sec)
        last_state = ""
        last_pages: tuple[int | None, int | None] = (None, None)

        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                raise MinerUError("MinerU poll cancelled", "TASK_CANCELLED")
            try:
                with get_limits().mineru_poll_slot(timeout=15.0):
                    resp = httpx.get(
                        f"{self._base}/api/v4/extract-results/batch/{batch_id}",
                        headers=self._headers(),
                        timeout=30.0,
                    )
                resp.raise_for_status()
                data = resp.json()
            except TimeoutError:
                logger.warning("MinerU poll rate-limit wait, retry")
                time.sleep(interval)
                continue
            except httpx.HTTPError as exc:
                logger.warning("MinerU poll error: %s", exc)
                time.sleep(interval)
                continue

            body = data.get("data") or {}
            extract_result = body.get("extract_result") or []
            if not extract_result:
                time.sleep(interval)
                continue

            item = extract_result[0]
            state = (item.get("state") or "").lower()
            prog = item.get("extract_progress") or {}
            extracted = prog.get("extracted_pages")
            total = prog.get("total_pages")
            if state != last_state or (extracted, total) != last_pages:
                logger.info(
                    "MinerU batch=%s state=%s pages=%s/%s",
                    batch_id,
                    state,
                    extracted,
                    total,
                )
                last_state = state
                last_pages = (extracted, total)
                if on_progress is not None:
                    try:
                        pct = 20
                        if isinstance(total, int) and total > 0 and isinstance(extracted, int):
                            pct = 20 + int(20 * min(1.0, extracted / total))
                        on_progress("parse", pct, f"mineru {state} pages={extracted}/{total}")
                    except Exception as exc:
                        # Async worker progress raises TASK_CANCELLED; normalize for graph mapping.
                        if getattr(exc, "error_code", None) == "TASK_CANCELLED":
                            raise MinerUError(str(exc), "TASK_CANCELLED") from exc
                        raise

            if state in _DONE:
                return item
            if state in _FAILED:
                err = item.get("err_msg") or state
                raise MinerUError(f"MinerU extract failed: {err}", "MINERU_EXTRACT_FAILED")
            time.sleep(interval)

        raise MinerUError(
            f"MinerU poll timeout after {self._cfg.mineru_poll_timeout_sec}s",
            "MINERU_TIMEOUT",
        )

    def _parse_result(self, item: dict) -> tuple[str, int | None, list[dict]]:
        markdown = ""
        page_count: int | None = None
        images: list[dict] = []

        prog = item.get("extract_progress") or {}
        if isinstance(prog.get("total_pages"), int):
            page_count = prog["total_pages"]

        zip_url = item.get("full_zip_url")
        if zip_url:
            markdown, images = self._download_zip(zip_url)
        elif isinstance(item.get("markdown"), str):
            markdown = item["markdown"]

        if not markdown.strip():
            raise MinerUError("MinerU returned empty markdown", "MINERU_EMPTY_MD")
        return markdown, page_count, images

    def _download_zip(self, zip_url: str) -> tuple[str, list[dict]]:
        try:
            resp = httpx.get(zip_url, timeout=180.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MinerUError(f"download zip failed: {exc}", "MINERU_ZIP_DOWNLOAD_FAILED") from exc

        markdown = ""
        images: list[dict] = []
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                names = zf.namelist()
                md_name = next((n for n in names if n.endswith("full.md") or n.endswith(".md")), None)
                if not md_name:
                    raise MinerUError(f"zip has no markdown file, entries={names[:20]}", "MINERU_EMPTY_MD")
                markdown = zf.read(md_name).decode("utf-8", errors="replace")
                img_idx = 0
                for n in names:
                    lower = n.lower()
                    if not lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
                        continue
                    rel = Path(n).as_posix()
                    try:
                        raw = zf.read(n)
                    except Exception:
                        raw = b""
                    images.append(
                        {
                            "image_id": Path(n).stem or f"img_{img_idx}",
                            "relative_path": rel,
                            "status": "skipped",
                            "caption": None,
                            "bytes": raw,
                        }
                    )
                    img_idx += 1
        except zipfile.BadZipFile as exc:
            raise MinerUError(f"invalid zip from MinerU: {exc}", "MINERU_BAD_ZIP") from exc

        return markdown, images
