from pathlib import Path

from docx import Document

md = Path("samples/sample.md").read_text(encoding="utf-8")
doc = Document()
for line in md.splitlines():
    t = line.strip()
    if not t:
        continue
    if t.startswith("# "):
        doc.add_heading(t[2:], level=1)
    elif t.startswith("## "):
        doc.add_heading(t[3:], level=2)
    else:
        doc.add_paragraph(t)
out = Path("samples/sample.docx")
doc.save(str(out))
print("docx_ok", out.stat().st_size)
