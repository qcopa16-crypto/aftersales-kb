"""Disk cache for per-image bytes keyed by parse ``content_hash``.

Purpose: let ``/internal/review`` run a VLM spot-check **without** the caller
(Java) shipping image bytes, by reusing the bytes already extracted during parse
(MinerU). Layout::

    {parse_cache_disk_dir}/images/{content_hash}/{stem}_{sha1(relative_path)[:8]}{ext}

The short path hash in the filename keeps two images with the same basename in
different subdirectories (``images/a.png`` vs ``images/sub/a.png``) distinct, so
a spot-check never reads the wrong image.

Bounded (``review_image_cache_max`` per doc, ``review_image_cache_max_docs`` docs
with mtime-based GC), size-capped (``vlm_max_image_bytes``), idempotent (existing
files are kept) and **silent-degrade** (write failures never fail a parse). Paths
are sanitized against directory traversal.

This module must NOT import ``app.*`` (architecture boundary gate).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.config import Settings

logger = logging.getLogger(__name__)

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_HASH_RE = re.compile(r"[^A-Za-z0-9_-]")
_ATTEMPTED_MARKER = ".attempted"
_write_lock = threading.Lock()


def _safe_hash(value: str) -> str:
    """Keep only ``[A-Za-z0-9_-]`` (content_hash is a hex digest)."""
    return _HASH_RE.sub("", value or "")[:64]


def _safe_name(relative_path: str) -> str:
    """Collapse ``relative_path`` to one safe filename (no path separators).

    Appends a short sha1 of the *full* relative_path so two images that share a
    basename but live in different subdirectories map to distinct files.
    """
    base = (relative_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        stem, ext = base.rsplit(".", 1)
    else:
        stem, ext = base, ""
    stem = _UNSAFE_RE.sub("_", stem).strip("._") or "img"
    ext = _UNSAFE_RE.sub("_", ext).strip("._")
    digest = hashlib.sha1((relative_path or "").encode("utf-8")).hexdigest()[:8]
    name = f"{stem[:100]}_{digest}"
    if ext:
        name = f"{name}.{ext}"
    return name[:140]


def _images_root(cfg: Settings) -> Path:
    return Path(cfg.parse_cache_disk_dir) / "images"


def _doc_dir(cfg: Settings, content_hash: str) -> Path:
    return _images_root(cfg) / _safe_hash(content_hash)


def _enabled(cfg: Settings) -> bool:
    # The cache physically lives under the parse disk-cache root, so it inherits
    # that master switch; `review_image_cache_enabled` can additionally disable
    # just the review-image tier.
    return bool(
        getattr(cfg, "review_image_cache_enabled", True)
        and getattr(cfg, "parse_cache_disk_enabled", True)
    )


def _gc_locked(root: Path, max_docs: int) -> None:
    """Bound the number of per-document dirs under ``images/`` (oldest first).

    Only ever deletes *sub-directories of* ``images/``; never touches anything
    outside the cache root. Best-effort; failures are ignored.
    """
    try:
        if max_docs <= 0 or not root.is_dir():
            return
        entries = [p for p in root.iterdir() if p.is_dir()]
        if len(entries) <= max_docs:
            return
        entries.sort(key=lambda p: p.stat().st_mtime)
        for p in entries[: len(entries) - max_docs]:
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def cache_review_images(
    content_hash: str,
    image_bytes_map: Mapping[str, bytes] | None,
    images: Sequence[Any] | None,
    cfg: Settings,
) -> int:
    """Persist image bytes under ``images/{content_hash}/``.

    Returns the number of files newly written. Never raises. When the payload has
    images but none are cacheable (no bytes / all oversized), an ``.attempted``
    marker is still written so ``has_cached_images`` reports True and the parse
    disk-cache is not invalidated forever.
    """
    try:
        if not _enabled(cfg):
            return 0
        key = _safe_hash(content_hash)
        if not key:
            return 0
        max_images = int(getattr(cfg, "review_image_cache_max", 12) or 0)
        if max_images <= 0:
            return 0
        max_bytes = int(getattr(cfg, "vlm_max_image_bytes", 0) or 0)

        rels = [str((img or {}).get("relative_path") or "") for img in (images or [])]
        rels = [r for r in rels if r]
        bytes_map = image_bytes_map or {}

        # Deterministic order from `images`; dedup by relative_path; drop empty
        # and oversized blobs *before* applying the cap so we keep `max_images`
        # usable images.
        candidates: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for rel in rels:
            if rel in seen:
                continue
            seen.add(rel)
            blob = bytes_map.get(rel)
            if not isinstance(blob, (bytes, bytearray)) or not blob:
                continue
            if max_bytes > 0 and len(blob) > max_bytes:
                continue
            candidates.append((rel, bytes(blob)))
        candidates = candidates[:max_images]

        if not candidates and not rels:
            return 0

        doc_dir = _doc_dir(cfg, key)
        written = 0
        with _write_lock:
            doc_dir.mkdir(parents=True, exist_ok=True)
            for rel, blob in candidates:
                dest = doc_dir / _safe_name(rel)
                if dest.exists():
                    continue
                tmp = dest.with_name(dest.name + ".tmp")
                tmp.write_bytes(blob)
                tmp.replace(dest)
                written += 1
            if not candidates:
                # Images present but nothing cacheable → remember the attempt so
                # the parse cache-hit guard does not force endless re-parses.
                marker = doc_dir / _ATTEMPTED_MARKER
                if not marker.exists():
                    marker.write_text("", encoding="utf-8")
            _gc_locked(
                _images_root(cfg), int(getattr(cfg, "review_image_cache_max_docs", 200) or 0)
            )
        return written
    except Exception:  # noqa: BLE001 - never fail a parse over a cache write
        logger.warning("review image cache write failed hash=%s", content_hash, exc_info=True)
        return 0


def has_cached_images(content_hash: str, cfg: Settings) -> bool:
    """Whether ``images/{content_hash}/`` holds a cached image or an attempt marker.

    Used by ``/internal/parse`` to decide if a *disk-cache hit* is still valid for
    the review VLM tier: a cached payload whose image bytes were never written
    (e.g. cached before this feature existed) must be re-parsed. An
    ``.attempted`` marker (images present but none cacheable) counts as "done", so
    a document that simply has no extractable image bytes is not re-parsed forever.

    Returns ``True`` (i.e. "keep the hit") when the feature is disabled, when the
    cap is 0, or on any error — never forces a re-parse it cannot satisfy. Cheap:
    a directory existence + one ``iterdir`` scan. Never raises.
    """
    try:
        if not _enabled(cfg):
            return True
        if int(getattr(cfg, "review_image_cache_max", 12) or 0) <= 0:
            return True
        key = _safe_hash(content_hash)
        if not key:
            return True
        doc_dir = _doc_dir(cfg, key)
        if not doc_dir.is_dir():
            return False
        return any(child.is_file() for child in doc_dir.iterdir())
    except OSError:
        return True


def load_review_image(content_hash: str, relative_path: str, cfg: Settings) -> bytes | None:
    """Read one cached image by ``content_hash`` + ``relative_path``.

    Returns ``None`` on any miss/error (caller marks ``tiers.vlm="unavailable"``).
    Traversal-safe: the resolved file must stay inside ``images/{content_hash}/``.
    """
    if not _enabled(cfg):
        return None
    key = _safe_hash(content_hash)
    if not key or not relative_path:
        return None
    try:
        root = _images_root(cfg).resolve()
        doc_dir = (root / key).resolve()
        if doc_dir != root and root not in doc_dir.parents:
            return None
        dest = (doc_dir / _safe_name(relative_path)).resolve()
        if dest.parent != doc_dir:
            return None
        if not dest.is_file():
            return None
        return dest.read_bytes()
    except OSError:
        return None
