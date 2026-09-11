import httpx
from pathlib import Path

env = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

base = env["LLM_API_BASE"].rstrip("/")
key = env["LLM_API_KEY"]
r = httpx.post(
    f"{base}/embeddings",
    headers={"Authorization": f"Bearer {key}"},
    json={"model": "embedding-3", "input": ["测试向量", "另一条"]},
    timeout=30,
)
print("status", r.status_code)
data = r.json()["data"]
print("n", len(data), "dim", len(data[0]["embedding"]))
