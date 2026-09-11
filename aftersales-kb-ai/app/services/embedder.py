"""Embedding backends: local BGE-M3 (FlagEmbedding), cloud OpenAI-compatible, fake for tests."""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class Embedder(ABC):
    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder(Embedder):
    """Deterministic hash embedding for tests / offline smoke. Not for production quality."""

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            seed = hashlib.sha256(text.encode("utf-8")).digest()
            vec = [0.0] * self._dim
            for i in range(self._dim):
                byte = seed[i % len(seed)]
                vec[i] = ((byte / 255.0) - 0.5) * 2.0
                seed = hashlib.sha256(seed + bytes([i % 256])).digest()
            # L2 normalize
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


class CloudEmbedder(Embedder):
    def __init__(self, base: str, api_key: str, model: str, dim: int, timeout: float = 60.0) -> None:
        self._base = base.rstrip("/")
        self._key = api_key
        self._model = model
        self._dim = dim
        self._timeout = timeout

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self._base or not self._key:
            raise RuntimeError("EMBED_CLOUD_BASE / EMBED_CLOUD_KEY not configured")
        resp = httpx.post(
            f"{self._base}/embeddings",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"model": self._model, "input": texts},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data_sorted = sorted(data, key=lambda x: x["index"])
        vectors = [row["embedding"] for row in data_sorted]
        for i, vec in enumerate(vectors):
            if len(vec) != self._dim:
                raise RuntimeError(f"embedding dim mismatch at {i}: got {len(vec)}, expect {self._dim}")
        return vectors


class LocalBgeEmbedder(Embedder):
    def __init__(self, model_path: str, device: str, dim: int) -> None:
        if not model_path:
            raise RuntimeError("EMBED_LOCAL_MODEL is empty")
        from FlagEmbedding import BGEM3FlagModel  # heavy import, lazy

        self._model = BGEM3FlagModel(model_path, use_fp16=False, device=device)
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        emb = self._model.encode(
            texts,
            batch_size=8,
            max_length=8192,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        vectors: list[list[float]] = [list(map(float, v)) for v in emb["dense_vecs"]]
        for i, vec in enumerate(vectors):
            if len(vec) != self._dim:
                raise RuntimeError(f"embedding dim mismatch at {i}: got {len(vec)}, expect {self._dim}")
        return vectors


def create_embedder(cfg: Settings) -> Embedder:
    provider = (cfg.embed_provider or "fake").lower()
    if provider == "fake":
        return FakeEmbedder(dim=cfg.embed_dim)
    if provider == "cloud":
        return CloudEmbedder(
            base=cfg.embed_cloud_base,
            api_key=cfg.embed_cloud_key,
            model=cfg.embed_cloud_model,
            dim=cfg.embed_dim,
        )
    if provider == "local":
        return LocalBgeEmbedder(
            model_path=cfg.embed_local_model,
            device=cfg.embed_local_device,
            dim=cfg.embed_dim,
        )
    raise RuntimeError(f"unknown EMBED_PROVIDER: {provider}")
