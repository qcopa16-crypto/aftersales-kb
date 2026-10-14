"""Canonical image-reference matching keys shared by ingest / VLM / coverage.

Leaf module: intentionally imports nothing from the project (avoids import
cycles). Three call sites decide whether an image is *referenced* by the
markdown, whether a caption is *injected* into it, and whether a caption is
*injectable* at all. They must agree on one口径, so the key derivation lives
here and nowhere else.

Matching contract (fixed, must not be loosened elsewhere):
  - a reference is normalized (whitespace-stripped, ``\\`` -> ``/``, leading
    ``./`` dropped, repeated ``//`` collapsed);
  - keys are ``[normalized full path, basename]`` in that order, deduped;
  - callers match exact (full path) first, then fall back to basename.
"""

from __future__ import annotations


def normalize_image_ref(path: str) -> str:
    """Canonical form of an image reference for cross-source matching.

    Strips surrounding whitespace, unifies ``\\`` to ``/``, drops a leading
    ``./`` and collapses repeated ``//``. A pure filename is returned as-is.
    """
    s = (path or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    while "//" in s:
        s = s.replace("//", "/")
    return s


def image_ref_keys(path: str) -> list[str]:
    """Deduped match keys for one image reference: ``[full path, basename]``.

    The basename is omitted when it is empty or identical to the normalized
    full path, so a plain filename yields a single key.
    """
    norm = normalize_image_ref(path)
    keys: list[str] = []
    if norm:
        keys.append(norm)
        base = norm.rsplit("/", 1)[-1]
        if base and base != norm:
            keys.append(base)
    return keys
