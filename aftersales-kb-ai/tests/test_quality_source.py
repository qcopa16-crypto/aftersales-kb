import os

os.environ.setdefault("VECTOR_STORE", "memory")
os.environ.setdefault("EMBED_PROVIDER", "fake")
os.environ.setdefault("EMBED_DIM", "32")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("MINERU_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_mineru, get_vector_store  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()

from app.main import app  # noqa: E402
from app.services.quality import build_quality  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": "test-token"}
cfg = Settings()


def test_quality_suggests_review_for_long_body():
    md = "# 手册\n\n" + ("正文内容。" * 5000)
    chunks = [(md, ["手册"])]
    q = build_quality(
        markdown=md,
        chunks=chunks,
        image_count=0,
        image_in_md_count=0,
        page_count=None,
        file_type="md",
        cfg=cfg,
    )
    assert q.suggest_review is True
    assert "product_manual_heuristic" in q.review_reasons
    assert q.markdown_chars == len(md)


def test_quality_clean_small_doc():
    md = "# 短文\n\n一句话。\n"
    q = build_quality(
        markdown=md,
        chunks=[(md, ["短文"])],
        image_count=0,
        image_in_md_count=0,
        page_count=1,
        file_type="md",
        cfg=cfg,
    )
    assert q.suggest_review is False
    assert q.chunk_count == 1
    assert q.chunks_with_heading_ratio == 1.0


def test_parse_response_includes_quality():
    md = "# 安装\n\n上电后 PWR 常亮。\n"
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-q", "markdown": md},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "quality" in body
    assert body["quality"]["chunk_count"] == len(body["chunks"])
    assert body["quality"]["markdown_chars"] > 0


def test_parse_rejects_bad_source_scheme():
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={
            "file_type": "md",
            "unit_id": "U-url",
            "source_url": "ftp://evil/x.md",
        },
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["error_code"] == "SOURCE_BAD_URL"


def test_parse_source_url_404():
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={
            "file_type": "md",
            "unit_id": "U-url404",
            "source_url": "http://127.0.0.1:9/missing.md",
        },
    )
    assert resp.status_code == 502
    # loopback is SSRF-blocked before connect
    assert resp.json()["detail"]["error_code"] in {
        "SOURCE_FETCH_FAILED",
        "SOURCE_EMPTY",
        "SOURCE_SSRF_BLOCKED",
    }
