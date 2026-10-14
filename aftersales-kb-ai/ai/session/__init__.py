"""Multi-turn session memory (MongoDB-backed).

This package is the AI side's *only* holder of conversation state. It must stay
dependency-free of ``app/*`` (see tests/test_architecture_boundaries.py) and must
never block the QA main path: any Mongo failure degrades to an empty history.

Security model (see plan `mongodb-session-memory-2026-10-11.md` §2):
  * turns are keyed by ``(owner_id, session_id)`` — double-key isolation;
  * ``owner_id`` is an opaque partition key issued by Java, never interpreted here;
  * history is only usable *after* ACL filtering (``history.filter_turns``);
  * read failure => empty history (never "read failed => skip filtering").
"""

from ai.session.history import filter_turns, render_history, strip_citations
from ai.session.store import (
    MongoSessionStore,
    NullSessionStore,
    SessionStoreBackend,
    SessionTurn,
    get_session_store,
    reset_session_store_for_tests,
)

__all__ = [
    "MongoSessionStore",
    "NullSessionStore",
    "SessionStoreBackend",
    "SessionTurn",
    "filter_turns",
    "get_session_store",
    "render_history",
    "reset_session_store_for_tests",
    "strip_citations",
]
