"""DOCX → markdown-ish plain text."""

from __future__ import annotations

from pathlib import Path


def docx_to_markdown(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    lines: list[str] = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading 1"):
            lines.append(f"# {text}")
        elif style.startswith("heading 2"):
            lines.append(f"## {text}")
        elif style.startswith("heading 3"):
            lines.append(f"### {text}")
        else:
            lines.append(text)

    for table in doc.tables:
        rows: list[list[str]] = []
        for row in table.rows:
            cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
            rows.append(cells)
        if not rows:
            continue
        header = rows[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in rows[1:]:
            # pad/truncate to header width
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            lines.append("| " + " | ".join(row[: len(header)]) + " |")
        lines.append("")

    return "\n\n".join(lines)
