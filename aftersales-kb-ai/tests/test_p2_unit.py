"""P2 unit tests: SSRF, table summary, heading enrich."""

from pathlib import Path

import pytest

from app.config import Settings
from app.services.chunker import enrich_chunk_for_embed, split_markdown
from app.services.source_fetch import SourceFetchError, _validate_url


def test_validate_url_blocks_loopback():
    with pytest.raises(SourceFetchError) as e:
        _validate_url("http://127.0.0.1/x.pdf")
    assert e.value.error_code == "SOURCE_SSRF_BLOCKED"


def test_validate_url_blocks_file_scheme():
    with pytest.raises(SourceFetchError) as e:
        _validate_url("file:///etc/passwd")
    assert e.value.error_code == "SOURCE_BAD_URL"


def test_table_chunk_has_nl_summary():
    md = """# 故障

| 代码 | 含义 |
| --- | --- |
| E01 | 电源故障 |
"""
    chunks = split_markdown(md, chunk_size=500, chunk_overlap=20)
    text = "\n".join(c.content for c in chunks)
    assert "【表格摘要】" in text
    assert "代码" in text


def test_enrich_heading_prefix():
    out = enrich_chunk_for_embed("上电后常亮。", ["安装", "电源"])
    assert out.startswith("[安装 > 电源]")
    assert "上电后常亮" in out
    # idempotent if already prefixed
    again = enrich_chunk_for_embed(out, ["安装", "电源"])
    assert again == out
