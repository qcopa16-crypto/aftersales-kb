"""真实 PDF → MinerU 云解析 → 质检门 → embed 全链路测试。

这是唯一需要外部云服务（MinerU）的路径，耗时较长，单独成脚本、按需运行。

用法：python scripts/e2e-ai-pdf.py
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BE = "http://127.0.0.1:8080"
from _e2e_creds import get_password

PASS_WORD = get_password()
PDF = "docs/demo-knowledge/product/H3C LA2608室内无线网关 用户手册-6W100-整本手册.pdf"
results = []


def http(method, path, token=None, body=None, timeout=60, raw_body=None, content_type=None):
    req = urllib.request.Request(BE + path, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    elif body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = raw_body if raw_body is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check(name, ok, detail=""):
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "   -> " + str(detail)))


def jbody(t):
    try:
        return json.loads(t)
    except Exception:
        return {}


def login(u):
    st, tx = http("POST", "/api/auth/login", body={"username": u, "password": PASS_WORD})
    return jbody(tx).get("data", {}).get("accessToken") if st == 200 else None


def batch_file(token, batch_id, unit_id):
    st, tx = http("GET", f"/api/knowledge/import/batches/{batch_id}", token=token)
    for f in jbody(tx).get("data", {}).get("files", []):
        if f.get("unitId") == unit_id:
            return f
    return {}


admin = login("admin")
check("admin 登录成功", bool(admin))

with open(PDF, "rb") as fh:
    pdf_bytes = fh.read()
print(f"    PDF 大小 = {len(pdf_bytes)} bytes")

boundary = "----kbPdf" + uuid.uuid4().hex
parts = [
    ("--" + boundary + "\r\n").encode(),
    b'Content-Disposition: form-data; name="files"; filename="e2e-pdf-manual.pdf"\r\n',
    b"Content-Type: application/pdf\r\n\r\n",
    pdf_bytes,
    b"\r\n",
    ("--" + boundary + "\r\n").encode(),
    b'Content-Disposition: form-data; name="category"\r\n\r\n',
    b"product",
    b"\r\n",
    ("--" + boundary + "--\r\n").encode(),
]
st, tx = http("POST", "/api/knowledge/import", token=admin, raw_body=b"".join(parts),
              content_type="multipart/form-data; boundary=" + boundary, timeout=120)
data = jbody(tx)
check("PDF 导入接口返回 200", st == 200 and data.get("code") == 0, data)
batch = data.get("data", {}).get("batchId")
unit = data.get("data", {}).get("items", [{}])[0].get("unitId")
print(f"    batchId={batch} unitId={unit}")

print("    等待 MinerU 云端解析（最长 6 分钟）...")
deadline = time.time() + 360
last = {}
while time.time() < deadline:
    last = batch_file(admin, batch, unit)
    stage, status = last.get("stage"), last.get("ingestStatus")
    if stage in ("quality_gate", "failed") or status in ("active", "failed"):
        break
    time.sleep(5)

print(f"    stage={last.get('stage')} ingestStatus={last.get('ingestStatus')} "
      f"progress={last.get('progress')} chunkCount={last.get('chunkCount')}")
check("MinerU 云端解析完成（未超时/未失败）", last.get("stage") != "failed",
      f"{last.get('errorCode')} / {last.get('errorMessage')}")
check("PDF 解析出切片（真实 MinerU 抽取成功）", (last.get("chunkCount") or 0) > 0, last.get("chunkCount"))
if last.get("stage") == "quality_gate" and last.get("ingestStatus") == "parsed":
    check("AI 判不准 → 停在质检门（未确认不 embed）", True, last)
    st, tx = http("POST", f"/api/knowledge/import/quality-gate/{unit}", token=admin,
                  body={"action": "confirm"})
    check("人工兜底接口返回 200", st == 200 and jbody(tx).get("code") == 0, jbody(tx))
    deadline = time.time() + 240
    while time.time() < deadline:
        last = batch_file(admin, batch, unit)
        if last.get("ingestStatus") in ("active", "failed"):
            break
        time.sleep(4)
    check("人工确认后完成向量化并变为 active",
          last.get("ingestStatus") == "active", f"{last.get('errorCode')} / {last.get('ingestStatus')}")
else:
    check("AI 判定通过 → 自动 embed 变 active（未走人工）",
          last.get("ingestStatus") == "active", last)
    check("AI 自动通过：批次详情暴露判定结论 reviewVerdict=auto_pass",
          last.get("reviewVerdict") == "auto_pass", last.get("reviewVerdict"))
    check("AI 自动通过：判定来源标记为 ai（留痕）",
          last.get("reviewSource") == "ai", last.get("reviewSource"))

failed = results.count(False)
print(f"\n===== 真实 PDF(MinerU) 链路: {len(results) - failed}/{len(results)} 通过, {failed} 失败 =====")
sys.exit(1 if failed else 0)
