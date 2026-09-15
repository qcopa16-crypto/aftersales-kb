from ai.ingest.content_cache import ParseResultCache, content_hash
from ai.generate.vlm_cache import CaptionCache


def test_content_hash_stable():
    a = content_hash(file_type="md", file_bytes=None, markdown="hello", chunk_size=512, chunk_overlap=50, use_vlm=False)
    b = content_hash(file_type="md", file_bytes=None, markdown="hello", chunk_size=512, chunk_overlap=50, use_vlm=False)
    c = content_hash(file_type="md", file_bytes=None, markdown="hello2", chunk_size=512, chunk_overlap=50, use_vlm=False)
    assert a == b
    assert a != c


def test_parse_cache_lru():
    cache = ParseResultCache(max_items=2)
    cache.put("a", {"x": 1})
    cache.put("b", {"x": 2})
    cache.put("c", {"x": 3})
    assert cache.get("a") is None
    assert cache.get("c") == {"x": 3}


def test_caption_cache_roundtrip():
    cache = CaptionCache(max_items=2)
    blob = b"\xff\xd8fake"
    assert cache.get(blob) is None
    cache.put(blob, "【图示】电源灯")
    assert cache.get(blob) == "【图示】电源灯"
