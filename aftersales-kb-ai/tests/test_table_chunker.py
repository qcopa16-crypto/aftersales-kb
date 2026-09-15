from ai.ingest.chunker import split_markdown


def test_table_kept_atomic_when_fits():
    md = """# 故障代码

| 代码 | 含义 | 处理 |
| --- | --- | --- |
| E01 | 电源故障 | 断电重启 |
| E02 | 网络断开 | 检查网线 |

正文说明。
"""
    chunks = split_markdown(md, chunk_size=500, chunk_overlap=20)
    table_chunks = [c for c in chunks if "| 代码 |" in c.content]
    assert len(table_chunks) >= 1
    # table body not split across chunks without header
    for c in table_chunks:
        if "E01" in c.content or "E02" in c.content:
            assert "| 代码 |" in c.content
            assert "| --- |" in c.content or "|---|" in c.content.replace(" ", "")


def test_oversized_table_splits_with_header_repeat():
    rows = "\n".join(f"| R{i} | 值{i} |" for i in range(80))
    md = f"# 表\n\n| 列A | 列B |\n| --- | --- |\n{rows}\n"
    chunks = split_markdown(md, chunk_size=200, chunk_overlap=10)
    table_chunks = [c for c in chunks if c.content.lstrip().startswith("|")]
    assert len(table_chunks) > 1
    for c in table_chunks:
        lines = [ln for ln in c.content.splitlines() if ln.strip()]
        assert lines[0].startswith("|")
        assert "列A" in lines[0]
        # second line is separator
        assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-", ":"}


def test_image_block_survives_near_table():
    md = """# 图表

![](images/panel.jpg)

| 灯 | 状态 |
| --- | --- |
| PWR | 绿常亮 |
"""
    chunks = split_markdown(md, chunk_size=400, chunk_overlap=20)
    text = "\n".join(c.content for c in chunks)
    assert "images/panel.jpg" in text
    assert "PWR" in text
