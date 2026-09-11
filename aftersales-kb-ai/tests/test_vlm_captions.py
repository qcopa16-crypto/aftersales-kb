import os

os.environ.setdefault("VECTOR_STORE", "memory")
os.environ.setdefault("EMBED_PROVIDER", "fake")
os.environ.setdefault("EMBED_DIM", "32")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("MINERU_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_mineru, get_vlm, get_vector_store  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()
get_vlm.cache_clear()

from app.main import app  # noqa: E402
from app.services.vlm import (  # noqa: E402
    VlmClient,
    guess_mime,
    inject_captions_into_markdown,
)

client = TestClient(app)
HEADERS = {"X-Internal-Token": "test-token"}


def test_inject_captions():
    md = "# 图\n\n![](images/a.jpg)\n\n正文\n"
    out = inject_captions_into_markdown(md, {"images/a.jpg": "PWR 常亮绿为正常"})
    assert "【图示】PWR 常亮绿为正常" in out
    assert "![](images/a.jpg)" in out


def test_inject_skips_missing_caption():
    md = "![](images/a.jpg)\n"
    out = inject_captions_into_markdown(md, {})
    assert "【图示】" not in out


def test_guess_mime():
    assert guess_mime("a.png") == "image/png"
    assert guess_mime("a.jpg") == "image/jpeg"


def test_vlm_disabled_on_describe():
    resp = client.post(
        "/internal/vlm/describe",
        headers=HEADERS,
        files={"file": ("a.jpg", b"\xff\xd8\xff", "image/jpeg")},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "VLM_DISABLED"


class _FakeVlm(VlmClient):
    def __init__(self) -> None:
        cfg = Settings(
            vlm_enabled=True,
            vlm_api_base="http://test",
            vlm_api_key="k",
            vlm_model="fake-vlm",
        )
        super().__init__(cfg)

    def describe_image(self, image_bytes: bytes, *, mime: str = "image/jpeg") -> str:
        return "【测试】前面板 PWR 绿灯常亮"


def test_parse_use_vlm_injects_caption(monkeypatch):
    # Simulate pdf branch by calling inject path via a direct unit of VlmClient
    # Full PDF path needs MinerU; here we verify describe + inject contract used by parse.
    vlm = _FakeVlm()
    assert vlm.available is True
    cap = vlm.describe_image(b"fakeimg")
    md = "![](images/x.jpg)\n"
    out = inject_captions_into_markdown(md, {"images/x.jpg": cap})
    assert "【图示】" in out
    assert "PWR" in out


def test_parse_md_without_vlm_no_caption():
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-novlm", "markdown": "# t\n\n![](images/a.jpg)\n\nbody\n", "use_vlm": "false"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "【图示】" not in body["markdown"]
    assert body["images"][0]["status"] == "skipped"
