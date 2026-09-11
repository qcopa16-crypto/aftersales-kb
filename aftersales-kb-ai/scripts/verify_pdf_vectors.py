from pymilvus import MilvusClient

c = MilvusClient(uri="http://127.0.0.1:19530")
uid = "P2-PDF-1789144070"
rows = c.query(
    collection_name="kb_ai_chunks",
    filter=f'unit_id == "{uid}"',
    output_fields=["chunk_index", "content"],
    limit=100,
)
print("pdf_rows", len(rows))
hits = [r for r in rows if "图示" in (r.get("content") or "")]
print("chunks_with_caption", len(hits))
if hits:
    print(hits[0]["content"][:240])
print("stats", c.get_collection_stats("kb_ai_chunks"))
