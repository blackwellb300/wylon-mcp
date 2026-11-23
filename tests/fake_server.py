"""Runs the real server with core.api_request replaced by a fake API (no network). Used by edit_flow.py."""
import json, os, sys
from wylon_mcp import core, server

MODELS = os.environ["FAKE_MODELS"].split(",")

def fake(method, base_url, path, api_key, payload=None, timeout=None, **kw):
    if path == "/models":
        return {"data": [{"id": m, "type": "image"} for m in MODELS]}
    if method == "POST":
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps([path, payload]) + "\n")
        return {"id": "t1", "status": "queued"}
    return {"id": "t1", "type": "image", "status": "running", "model": MODELS[0]}

core.api_request = fake
sys.argv = ["wylon-mcp"]
server.main()
