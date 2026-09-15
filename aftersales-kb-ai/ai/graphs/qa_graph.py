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
            from ai.retrieve.pipeline import (
                RetrieveError,
                RetrieveRequestView,
                run_retrieve_pipeline,
            )

            result = run_retrieve_pipeline(
                request=RetrieveRequestView(
                    question=state["question"],
                    top_k=state.get("top_k") or 8,
                ),
                embedder=embedder,
                store=store,
                lexical=get_lexical_index(),
                cfg=get_settings(),
            )
        except RetrieveError as exc:
            raise QaGraphError(str(exc), exc.error_code) from exc
        except Exception as exc:
            raise QaGraphError(f"retrieve failed: {exc}", "RETRIEVE_FAILED") from exc
        return {"chunks": result["chunks"]}

    def node_generate(state: QaState) -> QaState:
        contexts = state.get("contexts") or []
        if not contexts and state.get("chunks"):
            # build contexts from retrieved chunks (caller may filter before generate)
            contexts = [
                {"unit_id": c["unit_id"], "title": "", "content": c["content"]}
                for c in state["chunks"]
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
