"""Offline test of edit_image against a fake wylon API in an isolated HOME (nothing is billed)."""
import asyncio, base64, json, sys, tempfile
from pathlib import Path
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HOME = Path(tempfile.mkdtemp(prefix="wylon-home-"))
LOG = HOME / "posts.jsonl"

def posted():
    return [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []

async def main(offline_model):
    models = "Qwen/Qwen-Image-2512" if offline_model else "Qwen/Qwen-Image-2.1,Qwen/Qwen-Image-2512"
    png, webp_as_png, bmp, big = HOME / "a.png", HOME / "b.png", HOME / "c.bmp", HOME / "d.png"
    Image.new("RGB", (64, 800), "red").save(png); Image.new("RGB", (64, 64)).save(webp_as_png, "WEBP")
    Image.new("RGB", (64, 64)).save(bmp); big.write_bytes(png.read_bytes() + b"\0" * 16_000_000)
    ok = True
    def check(label, cond, extra=""):
        nonlocal ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + label, extra)
    env = {"HOME": str(HOME), "WYLON_API_KEY": "sk-test-000000000000", "FAKE_MODELS": models, "FAKE_LOG": str(LOG)}
    fake = str(Path(__file__).with_name("fake_server.py"))
    async with stdio_client(StdioServerParameters(command=sys.executable, args=[fake], env=env)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            async def edit(**a):
                return await s.call_tool("edit_image", {"prompt": "换成浅灰背景", "images": [str(png)], "wait_seconds": 0, "prompt_choice": "original", **a})
            if offline_model:
                e = await edit(); check("edit refused when 2.1 offline, nothing posted", e.isError and "未上线" in e.content[0].text and not posted(), e.content[0].text[:120]); return ok
            check("listed", "edit_image" in [t.name for t in (await s.list_tools()).tools])
            g = await s.call_tool("edit_image", {"prompt": "x", "images": [str(png)]})
            check("gated when unset", "needs_user_choice" in g.content[0].text and not posted())
            for label, a in {"missing file": {"images": ["/nonexistent.png"]}, "bmp": {"images": [str(bmp)]}, "too large data url": {"images": [str(big)]},
                             "no images": {"images": []}, "11 images": {"images": [str(png)] * 11}, "bad size": {"size": "999x999"},
                             "http url": {"images": ["http://example.com/a.png"]}, "private ip": {"images": ["https://192.168.1.2/a.png"]}, "empty prompt": {"prompt": " "}}.items():
                e = await edit(**a); check("rejected: " + label, e.isError and not posted(), e.content[0].text.replace("\n", " ")[:90])
            o = await edit(images=[str(png), str(webp_as_png), "https://example.com/x.jpg"], seed=7)
            check("submitted -> pending", not o.isError and '"pending"' in o.content[0].text, "" if not o.isError else o.content[0].text)
            path, p = posted()[-1]
            check("endpoint", path == "/contents/generations/tasks")
            check("payload", {k: p.get(k) for k in ("type", "model", "size", "n", "output_format", "seed")} == {"type": "image", "model": "Qwen/Qwen-Image-2.1", "size": "auto", "n": 1, "output_format": "png", "seed": 7})
            u = [i["image_url"] for i in p.get("images", [])]
            check("image order and mime by content", len(u) == 3 and u[0].startswith("data:image/png;base64,") and u[1].startswith("data:image/webp;base64,") and u[2] == "https://example.com/x.jpg")
            check("bytes intact", base64.b64decode(u[0].split(",")[1]) == png.read_bytes())
            check("no unsupported fields", "negative_prompt" not in p and "mask" not in p)
            check("only one submit", len(posted()) == 1)
    return ok

sys.exit(0 if asyncio.run(main("offline" in sys.argv)) else 1)
