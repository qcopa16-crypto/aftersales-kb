"""Embedding backends: local BGE-M3 (FlagEmbedding), cloud OpenAI-compatible, fake for tests."""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod

import httpx

from core.config import Settings

logger = logging.getLogger(__name__)


class EmbedError(RuntimeError):
    """Embedding backend failure with retry classification metadata."""

    def __init__(
        self,
        message: str,
        error_code: str = "EMBED_FAILED",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class Embedder(ABC):
    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def encode(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def supports_sparse(self) -> bool:
        """True when the backend can emit term-level sparse weights."""
        return False

    def encode_sparse(self, texts: list[str]) -> list[dict[str, float]]:
        """Sparse term→weight vectors. Default: empty (sparse path disabled)."""
        return [{} for _ in texts]


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
    def __init__(
        self, base: str, api_key: str, model: str, dim: int, timeout: float = 60.0
    ) -> None:
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
            raise EmbedError(
                "EMBED_CLOUD_BASE / EMBED_CLOUD_KEY not configured",
                "EMBED_EMPTY_DOCUMENT",
            )
        try:
            resp = httpx.post(
                f"{self._base}/embeddings",
                headers={"Authorization": f"Bearer {self._key}"},
                json={"model": self._model, "input": texts},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if 400 <= code < 500 and code != 429:
                # Client faults are permanent; do not amplify with retries.
                raise EmbedError(
                    f"embed HTTP {code}", "EMBED_CLIENT_ERROR", status_code=code
                ) from exc
            # 5xx/429 become retryable via error_code/status_code classification.
            raise EmbedError(f"embed HTTP {code}", "EMBED_FAILED", status_code=code) from exc
        except httpx.HTTPError as exc:
            # Timeouts / connection errors carry "timeout"/"connection" in the name → retryable.
            raise EmbedError(f"embed request failed: {exc}", "EMBED_FAILED") from exc
        data = resp.json()["data"]
        data_sorted = sorted(data, key=lambda x: x["index"])
        vectors = [row["embedding"] for row in data_sorted]
        for i, vec in enumerate(vectors):
            if len(vec) != self._dim:
                # Permanent payload mismatch — never retry.
                raise EmbedError(
                    f"embedding dim mismatch at {i}: got {len(vec)}, expect {self._dim}",
                    "EMBED_DIM_MISMATCH",
                )
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
                raise RuntimeError(
                    f"embedding dim mismatch at {i}: got {len(vec)}, expect {self._dim}"
                )
        return vectors

    @property
    def supports_sparse(self) -> bool:
        return True

    def encode_sparse(self, texts: list[str]) -> list[dict[str, float]]:
        """BGE-M3 lexical_weights (term→weight) for process-local sparse retrieval."""
        if not texts:
            return []
        emb = self._model.encode(
            texts,
            batch_size=8,
            max_length=8192,
            return_dense=False,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        return [dict(w) for w in emb["lexical_weights"]]


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
