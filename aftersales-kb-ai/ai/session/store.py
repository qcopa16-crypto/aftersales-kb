"""Session store: MongoDB-backed (opt-in) + Null fallback.

Follows the Protocol + singleton + reset pattern of ``ai/ingest/task_store.py``.

Degrade policy (plan §2.3 / §5):
  * ``session_memory_enabled=False`` -> ``NullSessionStore``
  * pymongo missing / ping fails / any op raises -> Null (read) or no-op (write)
  * a read failure returns an EMPTY list, never "unfiltered history"
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)

try:  # pymongo is optional: importing it must never break service startup
    from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

    _PYMONGO_AVAILABLE = True
except Exception:  # pragma: no cover - only when pymongo is absent
    ASCENDING = DESCENDING = None  # type: ignore[assignment]
    MongoClient = None  # type: ignore[assignment]
    ReturnDocument = None  # type: ignore[assignment]
    _PYMONGO_AVAILABLE = False


@dataclass
class SessionTurn:
    """One conversation turn (single document sub-element)."""

    turn_id: str
    type: str = "qa"  # qa | faq | clarify_ask | clarify_reference | chosen
    question: str = ""
    answer: str = ""
    authorized_unit_ids: list[str] = field(default_factory=list)
    referenced_unit_ids: list[str] = field(default_factory=list)
    chosen_unit_id: str | None = None
    faq_hit: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seq: int = 0
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_doc(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "question": self.question,
            "answer": self.answer,
            "authorized_unit_ids": list(self.authorized_unit_ids or []),
            "referenced_unit_ids": list(self.referenced_unit_ids or []),
            "chosen_unit_id": self.chosen_unit_id,
            "faq_hit": bool(self.faq_hit),
            "tokens": {
                "prompt": int(self.prompt_tokens or 0),
                "completion": int(self.completion_tokens or 0),
            },
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> SessionTurn:
        tokens = doc.get("tokens") or {}
        ts = doc.get("ts")
        if not isinstance(ts, datetime):
            ts = datetime.now(timezone.utc)
        return cls(
            turn_id=str(doc.get("turn_id") or ""),
            type=str(doc.get("type") or "qa"),
            question=str(doc.get("question") or ""),
            answer=str(doc.get("answer") or ""),
            authorized_unit_ids=[str(u) for u in (doc.get("authorized_unit_ids") or [])],
            referenced_unit_ids=[str(u) for u in (doc.get("referenced_unit_ids") or [])],
            chosen_unit_id=doc.get("chosen_unit_id"),
            faq_hit=bool(doc.get("faq_hit")),
            prompt_tokens=int(tokens.get("prompt") or 0),
            completion_tokens=int(tokens.get("completion") or 0),
            seq=int(doc.get("seq") or 0),
            ts=ts,
        )


class SessionStoreBackend(Protocol):
    """Backends (Mongo / future Redis-SQL) must implement this surface."""

    def append_turn(self, *, owner_id: str, session_id: str, turn: SessionTurn) -> int | None: ...

    def get_turns(self, *, owner_id: str, session_id: str, limit: int = 0) -> list[SessionTurn]: ...

    def healthy(self) -> bool: ...


class NullSessionStore:
    """Degraded store: writes are no-ops, reads are empty (fail-closed)."""

    def append_turn(self, *, owner_id: str, session_id: str, turn: SessionTurn) -> int | None:
        return None

    def get_turns(self, *, owner_id: str, session_id: str, limit: int = 0) -> list[SessionTurn]:
        return []

    def healthy(self) -> bool:
        return False


def _doc_id(owner_id: str, session_id: str) -> str:
    """Double-key isolation: the primary read key embeds BOTH owner and session."""
    return f"{owner_id}:{session_id}"


# Retention cap for the per-session ``turns`` array. Env-overridable (direct read,
# no config.py change) so the array can never grow unbounded. ``seq_counter`` stays
# monotonic: trimming the array does NOT reset or reuse sequence numbers.
_DEFAULT_SESSION_MAX_TURNS = 50
_MIN_SESSION_MAX_TURNS = 1


def _session_max_turns() -> int:
    """Resolve the retention cap; fall back to the default on missing/invalid env."""
    raw = os.getenv("SESSION_MAX_TURNS")
    if raw is None or raw.strip() == "":
        return _DEFAULT_SESSION_MAX_TURNS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "invalid SESSION_MAX_TURNS=%r, using default %d", raw, _DEFAULT_SESSION_MAX_TURNS
        )
        return _DEFAULT_SESSION_MAX_TURNS
    return max(_MIN_SESSION_MAX_TURNS, value)


class MongoSessionStore:
    """MongoDB-backed session store (single-document atomic turns array)."""

    def __init__(self, cfg: Any) -> None:
        if not _PYMONGO_AVAILABLE:
            raise RuntimeError("pymongo is not installed")
        uri = getattr(cfg, "session_mongo_uri", "") or ""
        if not uri:
            raise RuntimeError("session_mongo_uri is empty")
        timeout = int(getattr(cfg, "session_mongo_timeout_ms", 800) or 800)
        self._ttl_days = max(1, int(getattr(cfg, "session_memory_ttl_days", 30) or 30))
        self._client = MongoClient(
            uri,
            serverSelectionTimeoutMS=timeout,
            connectTimeoutMS=timeout,
            socketTimeoutMS=max(timeout, 2000),
        )
        # fail fast at construction so get_session_store() can degrade immediately
        self._client.admin.command("ping")
        db = self._client[getattr(cfg, "session_mongo_db", "aftersales_kb_ai")]
        self._coll = db[getattr(cfg, "session_mongo_collection", "chat_sessions")]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        try:
            self._coll.create_index([("owner_id", ASCENDING), ("updated_at", DESCENDING)])
            # TTL: expire_at is refreshed on every append
            self._coll.create_index("expire_at", expireAfterSeconds=0)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("session index creation failed: %s", exc)

    def append_turn(self, *, owner_id: str, session_id: str, turn: SessionTurn) -> int | None:
        try:
            doc_id = _doc_id(owner_id, session_id)
            now = datetime.now(timezone.utc)
            expire = now + timedelta(days=self._ttl_days)
            # 1) atomically take a unique seq (also refreshes TTL / updated_at)
            doc = self._coll.find_one_and_update(
                {"_id": doc_id},
                {
                    "$inc": {"seq_counter": 1},
                    "$set": {
                        "owner_id": owner_id,
                        "session_id": session_id,
                        "updated_at": now,
                        "expire_at": expire,
                    },
                    "$setOnInsert": {"created_at": now, "turns": []},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
                projection={"seq_counter": 1},
            )
            turn.seq = int((doc or {}).get("seq_counter") or 1)
            # 2) idempotency guard: same turn_id is pushed at most once.
            #    ``$slice: -N`` keeps only the newest N turns (bounded growth).
            max_turns = _session_max_turns()
            res = self._coll.update_one(
                {"_id": doc_id, "turns.turn_id": {"$ne": turn.turn_id}},
                {"$push": {"turns": {"$each": [turn.to_doc()], "$slice": -max_turns}}},
            )
            return turn.seq if (res.matched_count or res.modified_count) else None
        except Exception as exc:
            logger.warning(
                "session append degraded owner=%s session=%s: %s", owner_id, session_id, exc
            )
            return None

    def get_turns(self, *, owner_id: str, session_id: str, limit: int = 0) -> list[SessionTurn]:
        try:
            # Project only the tail of the array: never ship the whole history back.
            # ``limit > 0`` -> read exactly the newest ``limit`` (>= the caller's need);
            # otherwise -> read the newest ``max_turns`` (the retention cap).
            slice_n = limit if limit and limit > 0 else _session_max_turns()
            doc = self._coll.find_one(
                {"_id": _doc_id(owner_id, session_id)}, {"turns": {"$slice": -slice_n}}
            )
            if not doc:
                return []
            turns = [SessionTurn.from_doc(t) for t in (doc.get("turns") or [])]
            turns.sort(key=lambda t: (t.seq, t.ts))
            if limit and limit > 0:
                turns = turns[-limit:]
            return turns
        except Exception as exc:
            # read failure => EMPTY history (never "read failed -> skip filtering")
            logger.warning(
                "session read degraded owner=%s session=%s: %s", owner_id, session_id, exc
            )
            return []

    def healthy(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False


_store: SessionStoreBackend | None = None
_store_lock = threading.Lock()


def get_session_store() -> SessionStoreBackend:
    """Return the process-wide session store, degrading to Null on any failure."""
    global _store
    from core.config import get_settings

    cfg = get_settings()
    if not getattr(cfg, "session_memory_enabled", False):
        return NullSessionStore()
    with _store_lock:
        if _store is None:
            try:
                _store = MongoSessionStore(cfg)
                logger.info("session store ready: mongo")
            except Exception as exc:
                # do NOT cache the failure: a recovered Mongo is picked up next call
                logger.warning("session store unavailable, degrade to null: %s", exc)
                return NullSessionStore()
        return _store


def reset_session_store_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
