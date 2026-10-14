from functools import lru_cache

from core.config import get_settings
from ai.embed.embedder import Embedder, create_embedder
from ai.generate.llm import LlmClient
from ai.ingest.mineru_client import MinerUClient
from ai.retrieve.store import VectorStore, create_vector_store
from ai.generate.vlm import VlmClient


@lru_cache
def get_embedder() -> Embedder:
    return create_embedder(get_settings())


@lru_cache
def get_vector_store() -> VectorStore:
    store = create_vector_store(get_settings())
    store.ensure_collection()
    return store


@lru_cache
def get_mineru() -> MinerUClient:
    return MinerUClient(get_settings())


@lru_cache
def get_vlm() -> VlmClient:
    return VlmClient(get_settings())


@lru_cache
def get_llm() -> LlmClient:
    return LlmClient(get_settings())
