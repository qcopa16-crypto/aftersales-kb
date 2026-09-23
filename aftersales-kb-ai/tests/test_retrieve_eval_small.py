"""Offline unit tests for retrieve recall metrics."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ai.retrieve.recall_metrics import (  # noqa: E402
    aggregate,
    classify_miss,
    keyword_hit,
    score_item,
    unit_hit_rank,
)
from ai.retrieve.pipeline import extract_exact_tokens, fuse_rrf, rule_rerank  # noqa: E402
from ai.retrieve.pipeline import FuseHit, embed_rerank, unit_representative_order  # noqa: E402


def test_fuse_rrf_weighted() -> None:
    scores = fuse_rrf([["A"], ["B"]], k=60, weights=[1.0, 0.35])
    assert scores["A"] > scores["B"]


def test_exact_tokens_and_rerank() -> None:
    assert "E-05" in extract_exact_tokens("盖板没关好会报 E-05 吗")
    assert "LA2608" in extract_exact_tokens("LA2608 怎么恢复出厂")

    def _hit(uid: str, content: str, score: float) -> FuseHit:
        return FuseHit(
            chunk_id=uid + "-c",
            unit_id=uid,
            chunk_index=0,
            content=content,
            version_id="",
            heading_path=[],
            score_dense=0.0,
            score_lexical=0.0,
            score_fused=score,
            retrieval_paths=["dense"],
            acl_department="",
            acl_classification="",
        )

    gold = _hit("u1", "| E-05 | 盖板未关到位 | 全部盖板重新扣紧 |", 0.01)
    other = _hit("u2", "打印机通用说明，不含错误码。", 0.015)
    out = rule_rerank([other, gold], "盖板没关好会报哪个错误码 E-05？")
    assert out[0].unit_id == "u1"


def test_unit_representative_order() -> None:
    def _hit(uid: str, score: float, cid: str = "c") -> FuseHit:
        return FuseHit(
            chunk_id=cid,
            unit_id=uid,
            chunk_index=0,
            content="x",
            version_id="",
            heading_path=[],
            score_dense=0.0,
            score_lexical=0.0,
            score_fused=score,
            retrieval_paths=["dense"],
            acl_department="",
            acl_classification="",
        )

    hits = [
        _hit("pdf", 0.50, "a"),
        _hit("pdf", 0.49, "b"),
        _hit("faq", 0.48, "c"),
        _hit("pdf", 0.47, "d"),
    ]
    ordered = unit_representative_order(hits)
    # Units first: pdf best then faq best; extras after reps.
    assert ordered[0].chunk_id == "a"
    assert ordered[1].chunk_id == "c"
    assert ordered[2].chunk_id in {"b", "d"}


def test_embed_rerank_prefers_semantic_match() -> None:
    class _E:
        def encode(self, texts):
            # orthogonal-ish fake: first dim by keyword
            out = []
            for t in texts:
                v = [0.0, 0.0]
                if "卡纸" in t:
                    v[0] = 1.0
                else:
                    v[1] = 1.0
                out.append(v)
            return out

    def _hit(uid: str, content: str, score: float) -> FuseHit:
        return FuseHit(
            chunk_id=uid + "-c",
            unit_id=uid,
            chunk_index=0,
            content=content,
            version_id="",
            heading_path=[],
            score_dense=0.0,
            score_lexical=0.0,
            score_fused=score,
            retrieval_paths=["dense"],
            acl_department="",
            acl_classification="",
        )

    noisy = _hit("n", "无关说明文档", 0.02)
    good = _hit("g", "卡纸排查步骤", 0.015)
    out = embed_rerank(
        [noisy, good],
        question_vec=[1.0, 0.0],
        embedder=_E(),
        top_n=5,
        weight=0.5,
    )
    assert out[0].unit_id == "g"


def test_unit_hit_rank() -> None:
    hits = [{"unit_id": "a"}, {"unit_id": "b"}, {"unit_id": "c"}]
    assert unit_hit_rank(hits, ["b"]) == 2
    assert unit_hit_rank(hits, ["z"]) is None


def test_keyword_and_score() -> None:
    hits = [{"unit_id": "a", "content": "打印机 E-01 卡纸处理"}, {"unit_id": "b", "content": "无关"}]
    assert keyword_hit(hits, ["E-01"]) is True
    row = score_item(hits, {"id": "q", "gold_unit_ids": ["a"], "gold_keywords": ["卡纸"]})
    assert row["unit_hit"] and row["unit_rank"] == 1 and row["content_hit"]
    assert row["reciprocal_rank"] == 1.0


def test_aggregate_and_buckets() -> None:
    rows = [
        {"unit_rank": 1, "unit_hit": True, "content_hit": True, "keyword_hit": True, "reciprocal_rank": 1.0},
        {"unit_rank": 5, "unit_hit": True, "content_hit": True, "keyword_hit": True, "reciprocal_rank": 0.2},
        {"unit_rank": None, "unit_hit": False, "content_hit": False, "keyword_hit": False, "reciprocal_rank": 0.0},
    ]
    agg = aggregate(rows, ks=(1, 5))
    assert agg["unit_recall@1"] == round(1 / 3, 4)
    assert agg["unit_recall@5"] == round(2 / 3, 4)
    assert classify_miss(rows[0]) is None
    assert classify_miss(rows[1]) == "hit_low_rank"
    assert classify_miss(rows[2], hybrid_hit=True, dense_hit=False) == "lexical_gap"
