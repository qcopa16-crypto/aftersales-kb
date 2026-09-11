from pymilvus import MilvusClient

c = MilvusClient(uri="http://127.0.0.1:19530")
print("collections", c.list_collections())
print("has_kb_chunks", c.has_collection("kb_chunks"))
try:
    desc = c.describe_collection("kb_chunks")
    print("desc", desc)
except Exception as e:
    print("describe_err", type(e), e)
# query a few rows if any
try:
    rows = c.query(collection_name="kb_chunks", filter="", output_fields=["unit_id"], limit=3)
    print("sample_rows", rows)
except Exception as e:
    print("query_err", type(e), e)
