"""Architecture gate: domain layer must not import the HTTP app package."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AI_ROOT = ROOT / "ai"


def test_ai_does_not_import_app_package():
    offenders: list[str] = []
    for p in AI_ROOT.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if "from app." in s or s.startswith("import app"):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {s}")
    assert not offenders, "ai/* must not import app/*:\n" + "\n".join(offenders)


def test_core_does_not_import_app_or_ai():
    offenders: list[str] = []
    core = ROOT / "core"
    for p in core.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if re_app_or_ai(s):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {s}")
    assert not offenders, "core/* must stay dependency-free of app/ai:\n" + "\n".join(offenders)


def re_app_or_ai(s: str) -> bool:
    return (
        "from app." in s
        or s.startswith("import app")
        or "from ai." in s
        or s.startswith("import ai")
    )
