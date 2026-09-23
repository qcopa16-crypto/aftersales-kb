"""Offline tests for query expansion."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ai.retrieve.query_expand import expand_query_texts, expand_terms  # noqa: E402


def test_expand_terms_synonyms() -> None:
    terms = expand_terms("屏幕摔裂了还能免费保修吗？")
    assert any("碎" in t or "屏" in t for t in terms)
    assert any("保修" in t or "质保" in t for t in terms)


def test_expand_query_texts_includes_original() -> None:
    texts = expand_query_texts("打印机总是卡纸怎么办？")
    assert texts[0].startswith("打印机")
    assert len(texts) >= 2
    assert ("夹纸" in texts[1]) or ("卡纸" in texts[1])


def test_expand_ascii_codes() -> None:
    terms = expand_terms("E-05 是什么故障")
    assert "E-05" in [t.upper() for t in terms]


def test_expand_price_and_competitor() -> None:
    t1 = " ".join(expand_terms("渠道供货价和目录价是一回事吗？"))
    assert "供货价" in t1
    t2 = " ".join(expand_terms("怎么和客户对比竞品又不得罪人？"))
    assert "话术" in t2 or "竞品" in t2
