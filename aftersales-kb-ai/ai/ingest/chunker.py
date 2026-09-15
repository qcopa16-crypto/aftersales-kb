"""Markdown structural chunker: split by headings, then by char window."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_IMG_ONLY_RE = re.compile(r"^!\[[^\]]*\]\([^)]+\)\s*$")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?.;；])\s*")


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) >= 3


def _split_body_blocks(body: str) -> list[tuple[str, str]]:
    """Return list of (kind, text) where kind is 'table' | 'image' | 'prose'."""
    lines = body.splitlines()
    blocks: list[tuple[str, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if _IMG_ONLY_RE.match(stripped):
            blocks.append(("image", stripped))
            i += 1
            continue
        if _is_table_line(line):
            j = i
            while j < n and _is_table_line(lines[j]):
                j += 1
            table = "\n".join(lines[i:j]).strip()
            if table:
                blocks.append(("table", table))
            i = j
            continue
        # prose run until next table/image
        j = i
        while j < n and not _is_table_line(lines[j]) and not _IMG_ONLY_RE.match(lines[j].strip()):
            j += 1
        prose = "\n".join(lines[i:j]).strip()
        if prose:
            blocks.append(("prose", prose))
        i = j
    return blocks


def _split_oversized_table(table: str, size: int) -> list[str]:
    """Keep header+separator on every piece when a table exceeds chunk_size."""
    lines = [ln for ln in table.splitlines() if ln.strip()]
    if len(lines) < 3:
        return [table]
    header = lines[0].strip()
    sep = lines[1].strip()
    if not re.match(r"^\|[\s:\-|]+\|$", sep):
        return _window_split(table, size, 0)
    rows = lines[2:]
    pieces: list[str] = []
    buf_rows: list[str] = []
    prefix = f"{header}\n{sep}\n"

    def flush() -> None:
        nonlocal buf_rows
        if buf_rows:
            pieces.append(prefix + "\n".join(buf_rows))
            buf_rows = []

    for row in rows:
        trial = prefix + "\n".join(buf_rows + [row]) if buf_rows else prefix + row
        if len(trial) > size and buf_rows:
            flush()
            buf_rows = [row]
        else:
            buf_rows.append(row)
    flush()
    return pieces or [table]


def _split_section_body(body: str, size: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    for kind, text in _split_body_blocks(body):
        if kind == "image":
            pieces.append(text)
        elif kind == "table":
            if len(text) <= size:
                pieces.append(text)
            else:
                pieces.extend(_split_oversized_table(text, size))
        else:
            pieces.extend(_window_split(text, size, overlap))
    return pieces


@dataclass
class Chunk:
    chunk_index: int
    content: str
    heading_path: list[str] = field(default_factory=list)


def _split_sentences(text: str) -> list[str]:
    parts = [p for p in _SENT_SPLIT_RE.split(text) if p and p.strip()]
    return parts if parts else [text]


def _window_split(text: str, size: int, overlap: int) -> list[str]:
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    overlap = max(0, min(overlap, size // 2))
    sentences = _split_sentences(text)
    pieces: list[str] = []
    buf = ""

    for sent in sentences:
        while len(sent) > size:
            if buf:
                pieces.append(buf.strip())
                buf = ""
            pieces.append(sent[:size].strip())
            sent = sent[size - overlap :]
        candidate = f"{buf}{sent}" if buf else sent
        if len(candidate) <= size:
            buf = candidate
        else:
            if buf.strip():
                pieces.append(buf.strip())
            buf = sent if overlap == 0 else (buf[-overlap:] + sent if buf else sent)

    if buf.strip():
        pieces.append(buf.strip())
    return [p for p in pieces if p]


def _extract_images(markdown: str) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", markdown):
        alt, path = m.group(1), m.group(2).strip()
        if path in seen:
            continue
        seen.add(path)
        image_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", path)[-64:] or f"img_{len(images)}"
        images.append(
            {
                "image_id": image_id,
                "relative_path": path,
                "status": "skipped",
                "caption": None,
            }
        )
    return images


def _table_summary(table: str) -> str:
    """One-line NL summary of a markdown pipe table for retrieval."""
    lines = [ln for ln in table.splitlines() if _is_table_line(ln)]
    if not lines:
        return ""
    header_cells = [c.strip() for c in lines[0].strip("|").split("|")]
    data_rows = len(lines) - 2 if len(lines) >= 2 and re.match(r"^\|[\s:\-|]+\|$", lines[1].strip()) else max(0, len(lines) - 1)
    cols = "、".join(h for h in header_cells if h) or "未知列"
    return f"> 【表格摘要】列：{cols}；数据行约 {data_rows} 行。"


def enrich_chunk_for_embed(content: str, heading_path: list[str]) -> str:
    """Prefix heading path so vectors carry section context (no schema change)."""
    prefix = " > ".join(h for h in heading_path if h).strip()
    if not prefix:
        return content
    if content.lstrip().startswith(f"[{prefix}]"):
        return content
    return f"[{prefix}]\n{content}"


def split_markdown(
    markdown: str,
    *,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
) -> list[Chunk]:
    """Split markdown into chunks with heading paths; tables get NL summaries."""
    if not markdown or not markdown.strip():
        return []

    matches = list(_HEADING_RE.finditer(markdown))
    sections: list[tuple[list[str], str]] = []

    if not matches:
        sections.append(([], markdown))
    else:
        prefix = markdown[: matches[0].start()]
        if prefix.strip():
            sections.append(([], prefix))

        stack: list[str] = []
        for i, m in enumerate(matches):
            level = len(m.group(1))
            title = m.group(2).strip()
            while len(stack) >= level:
                stack.pop()
            stack.append(title)
            path = list(stack)

            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            body = markdown[start:end]
            sections.append((path, body))

    chunks: list[Chunk] = []
    for path, body in sections:
        body = body.strip()
        if not body:
            continue
        if _IMG_ONLY_RE.match(body):
            chunks.append(Chunk(chunk_index=0, content=body, heading_path=list(path)))
            continue
        # inject table summaries into body before windowing
        annotated_parts: list[str] = []
        for kind, text in _split_body_blocks(body):
            if kind == "table":
                summary = _table_summary(text)
                annotated_parts.append(f"{summary}\n\n{text}" if summary else text)
            else:
                annotated_parts.append(text)
        annotated = "\n\n".join(annotated_parts)
        for piece in _split_section_body(annotated, chunk_size, chunk_overlap):
            chunks.append(Chunk(chunk_index=0, content=piece, heading_path=list(path)))

    for idx, ch in enumerate(chunks):
        ch.chunk_index = idx
    return chunks
