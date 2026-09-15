"""Memory+fake load smoke for retrieve. Not a CI gate.

Usage:
  python scripts/bench_retrieve_smoke.py
  python scripts/bench_retrieve_smoke.py --n 50 --warmup 3
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("VECTOR_STORE", "memory")
os.environ.setdefault("EMBED_PROVIDER", "fake")
os.environ.setdefault("EMBED_DIM", "32")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("MINERU_ENABLED", "false")

from core.config import Settings  # noqa: E402
from schemas.ingest import AclFilter  # noqa: E402
from ai.retrieve.lexical import LexicalIndex  # noqa: E402
from ai.retrieve.pipeline import (  # noqa: E402
    RetrieveRequestView,
    reset_bulkhead_for_tests,
    reset_qps_for_tests,
    run_retrieve_pipeline,
)
from ai.retrieve.store import MemoryVectorStore, VectorItem  # noqa: E402

SOP = ROOT.parent / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
if not SOP.is_file():
    SOP = ROOT / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"


class FakeEmbedder:
    def encode(self, texts):
        import hashlib

        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([((h[i % len(h)] + i) % 255) / 255.0 - 0.5 for i in range(32)])
        return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--warmup", type=int, default=3)
    args = p.parse_args()

    reset_bulkhead_for_tests()
    reset_qps_for_tests()

    cfg = Settings(
        vector_store="memory",
        embed_provider="fake",
        embed_dim=32,
        retrieve_hybrid_enabled=True,
        retrieve_max_concurrency=32,
        retrieve_qps_limit=0.0,
    )
    store = MemoryVectorStore(dim=32)
    emb = FakeEmbedder()
    text = SOP.read_text(encoding="utf-8")[:800] if SOP.is_file() else "工单处理 SOP"
    vec = emb.encode([text])[0]
    store.publish_unit(
        "U-bench",
        [
            VectorItem(
                chunk_index=0,
                content=text,
                embedding=vec,
                acl_department="售后部",
                acl_classification="internal",
            )
        ],
    )
    lexical = LexicalIndex()
    request = RetrieveRequestView(
        question="工单处理 SOP",
        top_k=3,
        acl_filter=AclFilter(unit_ids=["U-bench"], department="售后部", classification="internal"),
    )

    for _ in range(max(0, args.warmup)):
        run_retrieve_pipeline(
            request=request, embedder=emb, store=store, lexical=lexical, cfg=cfg
        )

    samples: list[float] = []
    ok = 0
    empty = 0
    for _ in range(max(1, args.n)):
        t0 = time.perf_counter()
        result = run_retrieve_pipeline(
            request=request, embedder=emb, store=store, lexical=lexical, cfg=cfg
        )
        samples.append((time.perf_counter() - t0) * 1000)
        ok += 1
        if not result["chunks"]:
            empty += 1

    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    print(f"n={len(samples)} ok={ok} empty={empty}")
    print(f"took_ms p50={p50:.2f} p95={p95:.2f} min={samples[0]:.2f} max={samples[-1]:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
