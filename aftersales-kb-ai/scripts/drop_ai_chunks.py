from pymilvus import MilvusClient

c = MilvusClient(uri="http://127.0.0.1:19530")
name = "kb_ai_chunks"
if c.has_collection(name):
    c.drop_collection(name)
    print("dropped", name)
else:
    print("absent", name)
print("collections", c.list_collections())
