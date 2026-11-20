"""Submit one 5s H3 video via MCP, then call get_task until it finishes (as an agent would)."""
import asyncio, json, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    async with stdio_client(StdioServerParameters(command="uv", args=["run", "wylon-mcp"])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            t0 = time.time()
            res = await s.call_tool("create_video_task", {
                "prompt": "清晨薄雾中的山谷航拍，镜头缓慢前推，阳光穿过云层，伴随轻柔风声和鸟鸣", "prompt_choice": "original"})
            print(res.content[0].text, "isError:", res.isError, flush=True)
            if res.isError:
                return
            tid = json.loads(res.content[0].text)["task_id"]
            for i in range(12):
                c0 = time.time()
                res = await s.call_tool("get_task", {"task_id": tid},
                                        progress_callback=lambda p, t, m: print("  progress:", m, flush=True))
                out = json.loads(res.content[0].text) if not res.isError else res.content[0].text
                print(f"[get_task #{i+1}] call took {time.time()-c0:.0f}s, total {time.time()-t0:.0f}s ->",
                      out if res.isError else {k: out.get(k) for k in ("status", "task_status", "local_path", "bytes")}, flush=True)
                if res.isError or out.get("status") != "pending":
                    break

asyncio.run(main())
