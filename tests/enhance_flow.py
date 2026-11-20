"""Offline test of the prompt-enhance gate in an isolated HOME (no API key => nothing can be billed)."""
import asyncio, json, os, sys, tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HOME = tempfile.mkdtemp(prefix="wylon-home-")
SERVER = str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "wylon-mcp")

def body(res):
    t = res.content[0].text
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return t

def cfg():
    p = Path(HOME, ".wylon", "config.json")
    return json.loads(p.read_text()) if p.exists() else {}

async def main():
    env = {"HOME": HOME, "WYLON_API_KEY": ""}
    async with stdio_client(StdioServerParameters(command=SERVER, env=env)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            print("tools:", names)
            ok = True
            def check(label, cond, extra=""):
                nonlocal ok
                ok &= bool(cond)
                print(("PASS " if cond else "FAIL ") + label, extra)

            check("set_prompt_enhance listed", "set_prompt_enhance" in names)
            r1 = body(await s.call_tool("generate_image", {"prompt": "画只猫"}))
            check("image gated when unset", isinstance(r1, dict) and r1["status"] == "needs_user_choice" and r1["charged"] is False)
            r2 = body(await s.call_tool("create_video_task", {"prompt": "海边日落"}))
            check("video gated when unset", isinstance(r2, dict) and r2["status"] == "needs_user_choice")
            check("nothing submitted", not Path(HOME, ".wylon", "tasks.jsonl").exists())

            r3 = await s.call_tool("generate_image", {"prompt": "画只猫", "prompt_choice": "original"})
            check("per-request choice passes gate (reaches key check)", r3.isError and "API Key" in r3.content[0].text)
            check("per-request choice not saved", "prompt_enhance" not in cfg())

            m = body(await s.call_tool("set_prompt_enhance", {"mode": "on"}))
            check("set on saved", cfg().get("prompt_enhance") is True, m["message"])
            r4 = await s.call_tool("generate_image", {"prompt": "一只橘猫趴在窗台上，午后阳光", "original_prompt": "画只猫"})
            check("pref on passes gate", r4.isError and "API Key" in r4.content[0].text)
            lm = body(await s.call_tool("list_models", {}))
            check("list_models shows preference", lm["prompt_enhance_preference"] is True)

            await s.call_tool("set_prompt_enhance", {"mode": "off"})
            check("set off saved", cfg().get("prompt_enhance") is False)
            await s.call_tool("set_prompt_enhance", {"mode": "ask"})
            check("set ask clears", "prompt_enhance" not in cfg())
            r5 = body(await s.call_tool("generate_image", {"prompt": "画只狗"}))
            check("gated again after ask", isinstance(r5, dict) and r5["status"] == "needs_user_choice")
            print("\nsample gate message:\n", json.dumps(r1, ensure_ascii=False, indent=2))
            return ok

sys.exit(0 if asyncio.run(main()) else 1)
