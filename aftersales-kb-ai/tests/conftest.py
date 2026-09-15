"""Shared pytest env + fixtures for aftersales-kb-ai.

Must run BEFORE test modules import app.* — conftest is loaded first.
Business fixture is ONLY 工单处理SOP.md.
Do NOT setdefault ENABLE_PARSE_IDEMPOTENCY here (tests override per case).
"""
from __future__ import annotations

import os
from pathlib import Path

# Offline CI defaults (memory + fake). Tests may still override extras after import.
os.environ.setdefault("VECTOR_STORE", "memory")
os.environ.setdefault("EMBED_PROVIDER", "fake")
os.environ.setdefault("EMBED_DIM", "32")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("MINERU_ENABLED", "false")
os.environ.setdefault("PARSE_FAIL_ON_EMPTY", "true")

import hashlib  # noqa: E402

import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SOP_PATH = REPO_ROOT / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"


@pytest.fixture(scope="session")
def sop_path() -> Path:
    assert SOP_PATH.is_file(), f"missing fixture: {SOP_PATH}"
    return SOP_PATH


@pytest.fixture(scope="session")
def sop_text(sop_path: Path) -> str:
    return sop_path.read_text(encoding="utf-8")


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": "test-token"}


class FakeEmbedder:
    """Deterministic embedder for memory-store tests. No model download."""

    def __init__(self, dim: int = 32) -> None:
        self.dim = dim
        self.encode_calls = 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.encode_calls += 1
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([((h[i % len(h)] + i) % 255) / 255.0 - 0.5 for i in range(self.dim)])
        return out


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder(dim=int(os.environ.get("EMBED_DIM", "32")))
