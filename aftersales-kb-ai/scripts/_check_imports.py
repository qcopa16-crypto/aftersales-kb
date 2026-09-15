"""Import-check every project module. Exit 1 on any failure."""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    failed: list[tuple[str, str]] = []
    ok = 0
    for base in ("app", "core", "ai"):
        try:
            importlib.import_module(base)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed.append((base, f"{type(exc).__name__}: {exc}"))
        base_path = ROOT / base
        for m in pkgutil.walk_packages([str(base_path)], prefix=base + "."):
            name = m.name
            try:
                importlib.import_module(name)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed.append((name, f"{type(exc).__name__}: {exc}"))
    print(f"IMPORT_OK {ok}")
    print(f"IMPORT_FAIL {len(failed)}")
    for name, err in failed:
        print(f"FAIL {name} :: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
