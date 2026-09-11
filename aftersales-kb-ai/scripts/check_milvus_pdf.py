from pymilvus import MilvusClient

c = MilvusClient(uri="http://127.0.0.1:19530")
coll = "kb_ai_chunks"
uid = None
# find pdf unit from latest
# list a few unit_ids
try:
    rows = c.query(collection_name=coll, filter="", output_fields=["unit_id"], limit=50)
    ids = sorted({r.get("unit_id") for r in rows if r.get("unit_id")})
    print("sample_unit_ids", ids[-10:])
    pdf_ids = [i for i in ids if i and "PDF" in i]
    print("pdf_ids", pdf_ids)
    for u in pdf_ids[-3:]:
        r2 = c.query(collection_name=coll, filter=f'unit_id == "{u}"', output_fields=["chunk_index"], limit=5)
        print(u, "rows", len(r2), r2[:2])
except Exception as e:
    print("query_err", e)

# flush
try:
    c.flush(coll)
    print("flushed")
except Exception as e:
    print("flush_err", e)

# get_collection_stats
try:
    print("stats", c.get_collection_stats(coll))
except Exception as e:
    print("stats_err", e)
