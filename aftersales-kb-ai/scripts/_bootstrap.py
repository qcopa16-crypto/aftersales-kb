"""Shared bootstrap for scripts/*.py (path + env helpers).

Usage:
    from _bootstrap import setup_path, use_memory_fake, use_milvus_fake
    setup_path()
    use_memory_fake()  # or use_milvus_fake(collection=...)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def setup_path() -> Path:
    """Ensure service root is importable and cwd is service root."""
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        os.chdir(ROOT)
    except OSError:
        pass
    return ROOT


def use_memory_fake(*, token: str = "test-token") -> None:
    os.environ.setdefault("VECTOR_STORE", "memory")
    os.environ.setdefault("EMBED_PROVIDER", "fake")
    os.environ.setdefault("EMBED_DIM", "32")
    os.environ.setdefault("INTERNAL_TOKEN", token)
    os.environ.setdefault("MINERU_ENABLED", "false")


def use_milvus_fake(
    *,
    collection: str,
    uri: str = "http://127.0.0.1:19530",
    dim: int = 32,
    token: str = "test-token",
    active_map: str | Path | None = None,
) -> None:
    """Dedicated-collection Milvus + fake embed. Never point at production kb_chunks."""
    os.environ["VECTOR_STORE"] = "milvus"
    os.environ["MILVUS_URI"] = uri
    os.environ["MILVUS_COLLECTION"] = collection
    os.environ["EMBED_PROVIDER"] = "fake"
    os.environ["EMBED_DIM"] = str(dim)
    os.environ["INTERNAL_TOKEN"] = token
    os.environ["MINERU_ENABLED"] = "false"
    if active_map is not None:
        os.environ["MILVUS_ACTIVE_MAP_PATH"] = str(active_map)


def sop_path() -> Path:
    p = ROOT.parent / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
    if not p.is_file():
        p = ROOT / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
    return p
