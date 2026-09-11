from app.services.chunker import split_markdown


def test_split_by_headings_and_index():
    md = """# 产品手册

## 安装

先接电源。

### 注意

断电操作。

## 故障

红灯闪烁表示电源故障。
"""
    chunks = split_markdown(md, chunk_size=200, chunk_overlap=20)
    assert len(chunks) >= 3
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    paths = {tuple(c.heading_path) for c in chunks}
    assert ("产品手册", "安装") in paths or any("安装" in p for p in paths)
    assert all(c.content.strip() for c in chunks)


def test_long_paragraph_windows():
    long = "这是一段很长的说明。" * 80
    md = f"# 标题\n\n{long}\n"
    chunks = split_markdown(md, chunk_size=100, chunk_overlap=20)
    assert len(chunks) > 1
    assert all(len(c.content) <= 160 for c in chunks)


def test_empty_markdown():
    assert split_markdown("") == []
    assert split_markdown("   \n\n") == []


def test_image_only_block_kept():
    md = "# 图\n\n![](images/a.jpg)\n\n正文。\n"
    chunks = split_markdown(md, chunk_size=500, chunk_overlap=20)
    contents = "\n".join(c.content for c in chunks)
    assert "images/a.jpg" in contents
    assert "正文" in contents
