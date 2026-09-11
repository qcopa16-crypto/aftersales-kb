from functools import lru_cache

from app.config import get_settings
from app.services.embedder import Embedder, create_embedder
from app.services.llm import LlmClient
from app.services.mineru_client import MinerUClient
from app.services.vector_store import VectorStore, create_vector_store
from app.services.vlm import VlmClient


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
