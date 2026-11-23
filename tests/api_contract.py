"""Offline checks against the published wylon API contract: local sockets and fakes only (nothing is billed)."""
import asyncio, json, os, socket, sys, tempfile, threading, time, urllib.request
os.environ["HOME"] = tempfile.mkdtemp(prefix="wylon-home-"); os.environ["WYLON_API_KEY"] = "sk-test-000000000000"
from wylon_mcp import core, server

ok = True
def check(label, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + label, "" if cond else extra)

def serve(reply, hang=0.0):
    """One-shot HTTP responder; counts requests."""
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(8); hits = []
    def run():
        while True:
            c, _ = s.accept(); hits.append(c.recv(65536)); time.sleep(hang)
            if reply: c.sendall(reply)
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{s.getsockname()[1]}", hits

def http(code, body=b""):
    return b"HTTP/1.1 %d X\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (code, len(body)) + body

def err(code):
    return json.dumps({"error": {"code": code, "message": "m", "type": "t"}}).encode()

def call(method, reply, hang=0.0, base=None):
    url, hits = serve(reply, hang) if base is None else (base, [])
    try:
        return core.api_request(method, url, "/x", "k", payload={} if method == "POST" else None, timeout=0.6), None, hits
    except core.WylonError as e:
        return None, e, hits

UNSURE = core.UNCERTAIN_SUBMIT_HINT
# --- submit: anything without a clear answer must warn, and is never retried
for label, reply, hang in [("timeout", None, 1.5), ("dropped connection", None, 0), ("500", http(500, err("InternalServiceError")), 0),
                           ("502", http(502, b"bad"), 0), ("504", http(504), 0), ("bad json", http(200, b"oops"), 0), ("truncated body", b"HTTP/1.1 200 X\r\nContent-Length: 99\r\n\r\n{", 0)]:
    r, e, hits = call("POST", reply, hang)
    check(f"POST {label}: warns not to resubmit, sent once", e is not None and e.hint == UNSURE and len(hits) == 1, (r, e and e.hint, len(hits)))
free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
_, e, _ = call("POST", None, base=f"http://127.0.0.1:{port}")
check("POST refused: says nothing was sent", e and "没有发出" in e.hint, e and e.hint)
_, e, hits = call("POST", http(503, err("ServiceUnavailable"))); check("POST 503: not created, sent once", e.hint == core.HTTP_HINTS[503] and len(hits) == 1)
_, e, _ = call("POST", http(403, err("AccountOverdueError"))); check("403 AccountOverdueError = balance", "余额不足" in e.hint, e.hint)
_, e, _ = call("POST", http(403, err("AccessDenied"))); check("403 AccessDenied = permission + key type", "订阅密钥" in e.hint and "余额" not in e.hint, e.hint)
_, e, _ = call("POST", http(402, err("insufficient_balance"))); check("402 = balance", "余额不足" in e.hint)
_, e, _ = call("GET", http(404, err("NotFound.Task"))); check("404 task hint", "不存在" in e.hint)
_, e, hits = call("GET", None); check("GET dropped: retried, handled", e is not None and len(hits) == 3 and e.hint != UNSURE, len(hits))
for label, reply in [("empty", http(200)), ("text", http(200, b"ok")), ("null", http(200, b"null"))]:
    r, e, _ = call("DELETE", reply); check(f"DELETE 200 {label} = success", r == {} and e is None, e)
_, e, _ = call("DELETE", http(409, err("TASK_RUNNING"))); check("DELETE 409 running hint", "已经开始运行" in e.hint)

# --- submit without id / cancel
real = core.api_request
core.api_request = lambda *a, **k: {}
try: core.submit_task({"type": "image", "model": "m"}, "image", "p"); check("no id", False)
except core.WylonError as e: check("submit without id warns", e.hint == UNSURE, e.hint)
for status, cancelled in [("queued", True), ("running", False), ("succeeded", False), ("failed", False), ("expired", False)]:
    calls = []
    core.api_request = lambda m, b, p, k, **kw: calls.append(m) or {"status": status}
    r = core.cancel_task("t1")
    check(f"cancel {status}", (r["status"] == "cancelled") == cancelled and ("DELETE" in calls) == cancelled, (r, calls))

# --- result: actual size reported; a skewed local clock must not block the download
core.download_binary = lambda url, mx, dest, tid, **k: dest.parent.mkdir(parents=True, exist_ok=True) or dest.write_bytes(b"x")
core.api_request = lambda *a, **k: {"status": "succeeded", "file_status": "available", "type": "image", "model": "m", "width": 1184, "height": 896,
                                    "seed": 5, "data": [{"url": "https://x.example/a.png", "url_expires_at": 1}]}
r = core.poll_task("t2", "image", 0, None)
check("actual size + download", r["status"] == "succeeded" and r["actual_size"] == "1184x896" and r["seed"] == 5, r)
core.api_request = lambda *a, **k: {"status": "running", "type": "video"}
check("pending mentions 2h retention", "2 小时" in core.poll_task("t3", "video", 0, None)["result_retention"])
core.api_request = real

# --- ratio
H3, REF, IMG = core.H3_MODEL, core.REF2VA_MODEL, "https://example.com/a.png"
v = lambda model=H3, ratio=None, first=None, last=None, refs=(), vids=(), auds=(), dur=5: core.build_video_payload("p", model, dur, ratio, first, last, list(refs), list(vids), list(auds), None, None)
check("text only -> 16:9", v()["ratio"] == "16:9")
check("first frame -> adaptive (ratio omitted)", "ratio" not in v(first=IMG))
check("last frame -> adaptive", "ratio" not in v(last=IMG))
check("explicit ratio kept", v(first=IMG, ratio="9:16")["ratio"] == "9:16")
check("Ref2VA -> 16:9", v(REF, refs=[IMG])["ratio"] == "16:9")
try: v(ratio="adaptive"); check("adaptive needs a frame", False)
except core.WylonError: check("adaptive needs a frame", True)

# --- cost
core._PRICE_CACHE["prices"] = None
cost = lambda p: core.estimate_cost(p)["estimated_cost"]
def blocked(*a, **k): raise AssertionError("estimate_cost must not use the network")
keep, urllib.request.urlopen = urllib.request.urlopen, blocked
check("5s text video", cost(v()) == "¥2.50", cost(v()))
check("first+last frame", cost(v(first=IMG, last=IMG)) == "¥2.90")
check("ref video surcharge", cost(v(REF, vids=["https://example.com/a.mp4"])) == "¥2.50，另加参考视频 ¥0.50/秒", cost(v(REF, vids=["https://example.com/a.mp4"])))
check("image price", cost({"type": "image", "model": "Qwen/Qwen-Image-2.1"}) == "¥0.25")
check("2512 has no list price", "未列" in cost({"type": "image", "model": "Qwen/Qwen-Image-2512"}))
def offline(*a, **k): raise TimeoutError("t")
urllib.request.urlopen = offline
check("price fallback offline", core.list_prices() == (core.PRICE_SNAPSHOT, False))
urllib.request.urlopen = keep
for bad in ({}, {"models": []}, {"models": [{"name": "MiniMax H3", "pricing": [{"rates": {}}]}]}):
    try: core._parse_prices(bad); check("bad price file rejected", False, bad)
    except (ValueError, KeyError): check("bad price file rejected", True)

# --- tool schema
tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
props = lambda n: tools[n].inputSchema["properties"]
check("8 tools", len(tools) == 8, list(tools))
check("no negative_prompt for images", "negative_prompt" not in props("generate_image") and "negative_prompt" not in props("edit_image"))
check("video negative_prompt explains limits", "蒸馏版不支持" in props("create_video_task")["negative_prompt"]["description"])
check("ratio has no forced default", props("create_video_task")["ratio"]["default"] is None)
check("image payload never carries negative_prompt", "negative_prompt" not in core.build_image_payload("p", "Qwen/Qwen-Image-2.1", None, 1))

# --- subscription key: only chat models visible
core.live_models = lambda: {"deepseek-ai/DeepSeek-V4.1-Flash": "llm"}
core._PRICE_CACHE.update(at=time.time(), prices=(core.PRICE_SNAPSHOT, False))
check("list_models flags chat-only key", server.list_models()["note"] == server.KEY_TYPE_NOTE)
check("check command flags chat-only key", server._check() == 1)
print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
