"""Spawn wylon-mcp over stdio and exercise it like a real MCP client.

usage: uv run python tests/smoke_client.py [offline|image|video|get TASK_ID]
"""
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

def show(res):
    for c in res.content:
        if c.type == "text":
            print(c.text)
        else:
            print(f"<{c.type} {getattr(c,'mimeType','')} {len(getattr(c,'data',''))} b64 chars>")
    print("isError:", res.isError)

async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "offline"
    params = StdioServerParameters(command="uv", args=["run", "wylon-mcp"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print("server:", init.serverInfo.name, init.serverInfo.version, "| protocol", init.protocolVersion)
            print("instructions chars:", len(init.instructions or ""))
            tools = await s.list_tools()
            for t in tools.tools:
                print("-", t.name, "| params:", list(t.inputSchema.get("properties", {})))
            if mode == "offline":
                print("\n== list_models"); show(await s.call_tool("list_models", {}))
                print("\n== bad size"); show(await s.call_tool("generate_image", {"prompt": "x", "size": "999x999", "model": "Qwen/Qwen-Image-2512", "prompt_choice": "original"}))
                print("\n== H3 + refs"); show(await s.call_tool("create_video_task", {"prompt": "x", "ref_images": ["/tmp/a.png"], "prompt_choice": "original"}))
                print("\n== adaptive w/o frames"); show(await s.call_tool("create_video_task", {"prompt": "x", "ratio": "adaptive", "prompt_choice": "original"}))
                print("\n== missing local frame"); show(await s.call_tool("create_video_task", {"prompt": "x", "first_frame": "/nonexistent.png", "prompt_choice": "original"}))
                print("\n== bad task id"); show(await s.call_tool("get_task", {"task_id": "../etc", "wait_seconds": 0}))
                print("\n== recent"); show(await s.call_tool("list_recent_tasks", {"limit": 2}))
            elif mode == "image":
                show(await s.call_tool("generate_image", {
                    "prompt": "极简风商业海报，一杯冒着热气的拿铁放在木桌上，暖色晨光，画面顶部居中写着“wylon MCP 测试”",
                    "size": "1792x2400", "prompt_choice": "original"}))
            elif mode == "video":
                show(await s.call_tool("create_video_task", {
                    "prompt": "清晨薄雾中的山谷航拍，镜头缓慢前推，阳光穿过云层，伴随轻柔风声和鸟鸣", "prompt_choice": "original"}))
            elif mode == "get":
                show(await s.call_tool("get_task", {"task_id": sys.argv[2], "wait_seconds": 45}))

asyncio.run(main())
