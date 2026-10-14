"""LangGraph QA graph: retrieve (optional) → generate stream prep."""

from __future__ import annotations

import logging
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from ai.graphs.state import QaState
from ai.generate.llm import LlmClient, LlmError

logger = logging.getLogger(__name__)


class QaGraphError(Exception):
    def __init__(self, message: str, error_code: str = "QA_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


def build_qa_graph(
    *,
    embedder: Any | None,
    store: Any | None,
    llm: LlmClient,
    on_delta: Callable[[str], None] | None = None,
):
    """retrieve uses embedder+store when contexts empty; generate streams via on_delta."""

    def node_retrieve(state: QaState) -> QaState:
        # If caller already provided authorized contexts, skip retrieve.
        if state.get("contexts"):
            return {"chunks": [], "stage": "generate"}  # type: ignore[typeddict-item]
        if embedder is None or store is None:
            raise QaGraphError("embedder/store not configured for retrieve", "RETRIEVE_FAILED")
        try:
            from core.config import get_settings
            from ai.retrieve.lexical import get_lexical_index
            from ai.retrieve.sparse import get_sparse_index
            from ai.retrieve.reranker import get_reranker
            from ai.retrieve.query_rewrite import get_query_rewriter
            from ai.retrieve.pipeline import (
                RetrieveError,
                RetrieveRequestView,
                run_retrieve_pipeline,
            )

            cfg = get_settings()
            result = run_retrieve_pipeline(
                request=RetrieveRequestView(
                    question=state["question"],
                    top_k=state.get("top_k") or 8,
                ),
                embedder=embedder,
                store=store,
                lexical=get_lexical_index(),
                cfg=cfg,
                sparse=get_sparse_index(),
                reranker=get_reranker(cfg),
                query_rewriter=get_query_rewriter(cfg, llm),
            )
        except RetrieveError as exc:
            raise QaGraphError(str(exc), exc.error_code) from exc
        except Exception as exc:
            raise QaGraphError(f"retrieve failed: {exc}", "RETRIEVE_FAILED") from exc
        return {"chunks": result["chunks"]}

    def node_generate(state: QaState) -> QaState:
        contexts = state.get("contexts") or []
        if not contexts and state.get("chunks"):
            # caller may have pre-filtered; pack (dedup + budget + [n] citation order)
            from core.config import get_settings
            from ai.generate.context_pack import pack_contexts

            cfg = get_settings()
            packed = pack_contexts(
                state["chunks"],
                max_chars=int(getattr(cfg, "generate_pack_max_chars", 6000)),
                per_chunk_max=int(getattr(cfg, "generate_pack_per_chunk_max", 1500)),
                jaccard_threshold=float(getattr(cfg, "generate_pack_jaccard", 0.85)),
            )
            contexts = [
                {"unit_id": p.unit_id, "title": p.title, "content": p.content} for p in packed
            ]
        deltas: list[str] = []
        try:
            for d in llm.stream_answer(state["question"], [_ctx_obj(c) for c in contexts]):
                deltas.append(d)
                if on_delta:
                    on_delta(d)
        except LlmError as exc:
            raise QaGraphError(str(exc), exc.error_code) from exc
        return {"answer_deltas": deltas, "contexts": contexts}

    def _ctx_obj(c: dict):
        from schemas.ingest import GenerateContext

        return GenerateContext(
            unit_id=str(c.get("unit_id") or ""),
            title=str(c.get("title") or ""),
            content=str(c.get("content") or ""),
        )

    g = StateGraph(QaState)
    g.add_node("retrieve", node_retrieve)
    g.add_node("generate", node_generate)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()


def run_qa_graph(
    *,
    embedder: Any | None,
    store: Any | None,
    llm: LlmClient,
    question: str,
    contexts: list[dict] | None = None,
    top_k: int = 8,
    session_id: str = "",
    on_delta: Callable[[str], None] | None = None,
) -> dict:
    graph = build_qa_graph(embedder=embedder, store=store, llm=llm, on_delta=on_delta)
    init: QaState = {
        "question": question,
        "top_k": top_k,
        "contexts": list(contexts or []),
        "session_id": session_id,
    }
    return graph.invoke(init)
