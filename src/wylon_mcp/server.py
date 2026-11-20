"""wylon MCP server: image & video generation tools over stdio."""

from __future__ import annotations

import asyncio
import base64
import getpass
import io
import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__, core

INSTRUCTIONS = """\
wylon 云端生图与生视频（https://wylon.cn）。结果会自动下载到用户本机，返回 local_path。

使用规则：
- 生成会按用户的 wylon 账户计费。用户只是随口提到、不确定要不要生成时，先确认再调用。
- 默认省钱：图片一次一张；视频默认 5 秒。要生成超过 3 张图或超过 1 条视频，先和用户确认数量。
- 不要因为等待超时就重新提交。generate_image / create_video_task 一旦返回 task_id，说明已提交成功，
  之后只用 get_task 和同一个 task_id 继续等待或找回结果。中断后找不到 task_id 时，先调用 list_recent_tasks。
- 视频通常需要 1~5 分钟：先 create_video_task 拿到 task_id，再反复调用 get_task（每次默认等约 25 秒）直到完成。
- 报错里带“建议”时，按建议修正参数后再试；余额不足（402）、限流（429）、鉴权失败（401/403）不要自动重试，告诉用户。
- 本地参考图、首尾帧传绝对路径；参考视频只支持公网 HTTPS 链接。
- 交付时告诉用户本地文件路径、模型、尺寸或时长、最终提示词和 task_id。云端链接是短效链接，以本地文件为准。
- 不要在对话里索要或回显 API Key；未配置时按报错提示引导用户自行配置。
- 提示词增强（是否把用户的简短描述扩写成完整提示词）由用户决定：用户还没选过时，生成工具会返回
  needs_user_choice 且不扣费，按返回的说明询问用户、用 set_prompt_enhance 保存后再提交。
  偏好为 on 时先扩写再提交，并把用户原话填进 original_prompt；为 off 时严格按原话提交。
"""

ENHANCE_GUIDE = {
    "image": "主体 + 场景 + 风格/画风 + 光线色调 + 构图/景别；画面里要出现的文字用引号写明原文和位置，例如：画面顶部居中写着“秋季上新”。",
    "video": "主体 + 动作 + 镜头运动（推、拉、摇、跟、航拍）+ 环境氛围 + 声音描述（模型会生成音效和人声，例如“伴随轻柔的雨声与远处雷鸣”）。",
}

PromptChoice = Literal["original", "enhanced"]


def _enhance_gate(kind: str, prompt: str, prompt_choice: Optional[str]) -> Optional[Dict[str, Any]]:
    """If the user has never chosen and didn't say so in this request, stop before billing and ask."""
    if prompt_choice is not None or core.get_prompt_enhance() is not None:
        return None
    return {
        "status": "needs_user_choice",
        "charged": False,
        "message": (
            "本次没有提交，也没有扣费。用户还没设置是否扩写提示词，请先询问用户："
            "1) 把用户的原话和你扩写后的版本都展示给用户，请用户选择“用扩写版”还是“按原话”；"
            "2) 告诉用户：会记住这个选择，以后不再询问，想改随时说“以后不要扩写”或“以后帮我扩写”；"
            "3) 用户选择后，先调用 set_prompt_enhance 保存（扩写选 on，按原话选 off），"
            "再用所选版本重新调用本工具；如果用扩写版，把用户原话填进 original_prompt。"
        ),
        "user_prompt": prompt,
        "how_to_enhance": ENHANCE_GUIDE[kind],
    }


def _enhance_info(prompt: str, original_prompt: Optional[str]) -> Dict[str, Any]:
    enhanced = bool(original_prompt and original_prompt.strip() and original_prompt.strip() != prompt.strip())
    info: Dict[str, Any] = {"prompt_enhance_preference": core.get_prompt_enhance(), "prompt_enhanced": enhanced}
    if enhanced:
        info["original_prompt"] = original_prompt
        info["delivery_note"] = "交付时注明“已扩写”，并把最终使用的提示词告诉用户，方便对比调整。"
    return info

mcp = FastMCP("wylon", instructions=INSTRUCTIONS, log_level="WARNING")
mcp._mcp_server.version = __version__  # report our version, not the SDK's

MAX_WAIT = 50  # keep each tool call under the common 60s client timeout
PREVIEW_MAX_SIDE = 512

ImageModel = Literal["Qwen/Qwen-Image-2.1", "Qwen/Qwen-Image-2512"]
VideoModel = Literal["MiniMaxAI/MiniMax-H3", "MiniMaxAI/MiniMax-H3-Ref2VA"]
VideoRatio = Literal["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"]


def _fail(exc: core.WylonError) -> ToolError:
    return ToolError(exc.render())


def _text(data: Dict[str, Any]) -> TextContent:
    return TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))


def _preview(path: str) -> Optional[ImageContent]:
    """Small JPEG thumbnail so the model can see the result without a multi-MB base64 blob."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
        return ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode("ascii"),
                            mimeType="image/jpeg")
    except Exception:
        return None


async def _wait_for_task(ctx: Optional[Context], task_id: str, kind: Optional[str],
                         wait_seconds: float, out_dir: Optional[str]) -> Dict[str, Any]:
    """Poll in short async steps so progress notifications and cancellation keep working."""
    start = time.monotonic()
    deadline = start + max(0.0, min(wait_seconds, MAX_WAIT))
    delay = 2.0
    while True:
        result = await anyio.to_thread.run_sync(core.poll_task, task_id, kind, 0, out_dir)
        remaining = deadline - time.monotonic()
        if result["status"] != "pending" or remaining <= 0:
            return result
        if ctx is not None:
            try:
                elapsed = time.monotonic() - start
                await ctx.report_progress(elapsed, None,
                                          f"任务 {task_id} {result.get('task_status')}，已等待 {int(elapsed)} 秒")
            except Exception:
                pass
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 1.4, 8.0)


def _result_content(result: Dict[str, Any], preview: bool) -> List[Any]:
    content: List[Any] = [_text(result)]
    if preview and result.get("status") == "succeeded" and result.get("kind") == "image":
        thumb = _preview(result["local_path"])
        if thumb:
            content.append(thumb)
    return content


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(title="列出 wylon 模型", readOnlyHint=True, openWorldHint=False))
def list_models() -> Dict[str, Any]:
    """列出 wylon 可用的生图/生视频模型、每个模型支持的尺寸、比例、时长和素材限制。参数拿不准时先调用它。"""
    api_key, base_url, source = core.resolve_credentials()
    catalog = json.loads(json.dumps(core.MODEL_CATALOG))
    live = core.live_models() if api_key else None
    for group in ("image_models", "video_models"):
        for m in catalog[group]:
            m["available"] = None if live is None else m["id"] in live
    if live is not None and core.DEFAULT_IMAGE_MODEL not in live:
        catalog["note"] = f"默认生图模型 {core.DEFAULT_IMAGE_MODEL} 当前不可用；不传 model 时会自动改用可用的生图模型。"
    return {
        **catalog,
        "api_key_configured": bool(api_key),
        "output_dir": str(core.output_dir(None)),
        "prompt_enhance_preference": core.get_prompt_enhance(),
    }


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="wylon 生图", readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=False, openWorldHint=True))
async def generate_image(
    prompt: Annotated[str, Field(description="生图提示词。画面里要出现的文字用引号写明原文和位置。")],
    size: Annotated[Optional[str], Field(
        description="精确分辨率，必须是所选模型支持的尺寸。Qwen-Image-2.1：2048x2048(1:1)、2752x1536(≈16:9)、"
                    "1536x2752(≈9:16)、2400x1792(4:3)、1792x2400(3:4)、2528x1696(3:2)、1696x2528(2:3)。"
                    "不传则用服务端默认尺寸。")] = None,
    model: Annotated[Optional[ImageModel], Field(
        description="生图模型，可选。不传时用 Qwen/Qwen-Image-2.1，它不可用时自动换成在线的模型并按宽高比调整尺寸。"
                    "用户点名时才指定。")] = None,
    negative_prompt: Annotated[Optional[str], Field(description="负向提示词，可选。")] = None,
    seed: Annotated[Optional[int], Field(description="随机种子，可选，用于复现。", ge=0)] = None,
    output_dir: Annotated[Optional[str], Field(
        description="保存目录的绝对路径，可选。默认 ~/wylon_outputs（或环境变量 WYLON_OUTPUT_DIR）。")] = None,
    wait_seconds: Annotated[int, Field(description="本次调用最多等待秒数，超时返回 pending 和 task_id。默认 25 秒，"
                                        "适配超时较短（约 30 秒）的客户端。", ge=0, le=MAX_WAIT)]
    = 25,
    preview: Annotated[bool, Field(description="成功时是否附带一张小尺寸预览图。")] = True,
    original_prompt: Annotated[Optional[str], Field(
        description="提示词经过扩写时，填用户的原话；按原话提交时不填。")] = None,
    prompt_choice: Annotated[Optional[PromptChoice], Field(
        description="仅当用户在本次请求里明确说了“按原话/不要改”（original）或“帮我扩写/润色”（enhanced）时填写，"
                    "只对本次生效、不保存偏好。")] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> List[Any]:
    """用 wylon 生成一张图片（计费），完成后自动下载到本地并返回 local_path 与预览图。
    如果返回 status=pending，说明已提交成功，用 get_task 和返回的 task_id 继续等待，不要重新调用本工具。
    如果返回 status=needs_user_choice，说明没有提交、没有扣费，按返回说明先询问用户是否扩写提示词。"""
    gate = _enhance_gate("image", prompt, prompt_choice)
    if gate:
        return [_text(gate)]
    try:
        payload = await anyio.to_thread.run_sync(
            core.build_image_payload, prompt, model, size, negative_prompt, seed)
        tid = await anyio.to_thread.run_sync(core.submit_task, payload, "image", prompt)
    except core.WylonError as exc:
        raise _fail(exc)
    try:
        result = await _wait_for_task(ctx, tid, "image", wait_seconds, output_dir)
    except core.WylonError as exc:
        exc.task_id = exc.task_id or tid
        raise _fail(exc)
    result.update({"model": result.get("model") or payload["model"],
                   "size": payload.get("size", "server_default"), "prompt": prompt})
    if payload.get("_notes"):
        result["notes"] = payload["_notes"]
    result.update(_enhance_info(prompt, original_prompt))
    return _result_content(result, preview)


@mcp.tool(annotations=ToolAnnotations(title="wylon 提交视频任务", readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=False, openWorldHint=True))
async def create_video_task(
    prompt: Annotated[str, Field(
        description="视频提示词：主体 + 动作 + 镜头运动 + 环境氛围 + 声音（模型会生成音效和人声）。"
                    "Ref2VA 模型里用 <Picture 1>、<Video 1>、<Audio 1> 按传入顺序引用素材。")],
    model: Annotated[VideoModel, Field(
        description="MiniMaxAI/MiniMax-H3（默认）：文生视频、首帧/首尾帧图生视频。"
                    "MiniMaxAI/MiniMax-H3-Ref2VA：多张参考图/视频/音频，保持人物或商品一致。")]
    = "MiniMaxAI/MiniMax-H3",
    duration: Annotated[int, Field(description="时长 5~15 秒，默认 5 秒（最省钱）。", ge=5, le=15)] = 5,
    ratio: Annotated[VideoRatio, Field(
        description="宽高比，默认 16:9。adaptive 只能在 MiniMax-H3 传了首/尾帧时使用（跟随图片比例）。")] = "16:9",
    first_frame: Annotated[Optional[str], Field(
        description="首帧图片：本地绝对路径或公网 HTTPS 链接。仅 MiniMax-H3。")] = None,
    last_frame: Annotated[Optional[str], Field(
        description="尾帧图片：本地绝对路径或公网 HTTPS 链接。仅 MiniMax-H3。")] = None,
    ref_images: Annotated[Optional[List[str]], Field(
        description="参考图列表（最多 9 张），本地绝对路径或公网 HTTPS 链接。仅 Ref2VA。")] = None,
    ref_videos: Annotated[Optional[List[str]], Field(
        description="参考视频列表（最多 3 个），只支持公网 HTTPS 的 MP4/MOV 链接。仅 Ref2VA。")] = None,
    ref_audios: Annotated[Optional[List[str]], Field(
        description="参考音频列表（最多 3 段），本地绝对路径或公网 HTTPS 的 WAV/MP3。仅 Ref2VA。")] = None,
    negative_prompt: Annotated[Optional[str], Field(description="负向提示词，可选。")] = None,
    seed: Annotated[Optional[int], Field(description="随机种子，可选。", ge=0)] = None,
    original_prompt: Annotated[Optional[str], Field(
        description="提示词经过扩写时，填用户的原话；按原话提交时不填。")] = None,
    prompt_choice: Annotated[Optional[PromptChoice], Field(
        description="仅当用户在本次请求里明确说了“按原话/不要改”（original）或“帮我扩写/润色”（enhanced）时填写，"
                    "只对本次生效、不保存偏好。")] = None,
) -> Dict[str, Any]:
    """提交一个 wylon 生视频任务（计费），立即返回 task_id。视频通常需要 1~5 分钟，
    之后用 get_task 传入 task_id 等待并下载结果。同一个需求只提交一次，不要因为没拿到结果就重复提交。
    如果返回 status=needs_user_choice，说明没有提交、没有扣费，按返回说明先询问用户是否扩写提示词。"""
    gate = _enhance_gate("video", prompt, prompt_choice)
    if gate:
        return gate
    try:
        payload = core.build_video_payload(
            prompt, model, duration, ratio, first_frame, last_frame,
            ref_images or [], ref_videos or [], ref_audios or [], negative_prompt, seed,
        )
        tid = await anyio.to_thread.run_sync(core.submit_task, payload, "video", prompt)
    except core.WylonError as exc:
        raise _fail(exc)
    return {
        "status": "submitted", "kind": "video", "task_id": tid, "model": model,
        "duration": duration, "ratio": ratio,
        "next_step": "调用 get_task(task_id) 等待并下载结果；一般需要 1~5 分钟，可多次调用。",
        "prompt": prompt,
        **_enhance_info(prompt, original_prompt),
    }


@mcp.tool(annotations=ToolAnnotations(title="wylon 提示词增强偏好", readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=False))
def set_prompt_enhance(
    mode: Annotated[Literal["on", "off", "ask"], Field(
        description="on：以后提交前都扩写提示词；off：以后都按用户原话提交；ask：清除偏好，下次生成前重新询问。")],
) -> Dict[str, Any]:
    """保存用户“是否扩写提示词”的偏好，之后所有会话都生效（与 wylon-media 技能共用）。
    只在用户做出选择或明确要求修改时调用，例如“以后不要扩写”“以后帮我扩写”。不产生费用。"""
    enabled = {"on": True, "off": False, "ask": None}[mode]
    core.set_prompt_enhance(enabled)
    messages = {
        True: "已开启提示词增强：之后提交前会在不改变原意的前提下扩写提示词。",
        False: "已关闭提示词增强：之后将严格按用户原话提交。",
        None: "已清除提示词增强偏好：下次生成前会重新询问用户。",
    }
    return {"prompt_enhance_preference": enabled, "message": messages[enabled]}


@mcp.tool(structured_output=False, annotations=ToolAnnotations(title="wylon 查询/找回任务", readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=True))
async def get_task(
    task_id: Annotated[str, Field(description="generate_image 或 create_video_task 返回的 task_id。")],
    wait_seconds: Annotated[int, Field(
        description="本次最多等待秒数，0 表示只查一次。未完成会返回 pending，可再次调用。默认 25 秒。", ge=0, le=MAX_WAIT)] = 25,
    output_dir: Annotated[Optional[str], Field(description="保存目录的绝对路径，可选，默认 ~/wylon_outputs。")] = None,
    preview: Annotated[bool, Field(description="图片任务成功时是否附带小尺寸预览图。")] = True,
    ctx: Context = None,  # type: ignore[assignment]
) -> List[Any]:
    """查询 wylon 任务状态；完成后自动下载到本地并返回 local_path（已下载过的不会重复下载）。
    也用于找回中断的任务。不会产生新的生成费用。"""
    try:
        result = await _wait_for_task(ctx, task_id, None, wait_seconds, output_dir)
    except core.WylonError as exc:
        raise _fail(exc)
    return _result_content(result, preview)


@mcp.tool(annotations=ToolAnnotations(title="wylon 取消任务", readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=True, openWorldHint=True))
async def cancel_task(
    task_id: Annotated[str, Field(description="要取消的 task_id。")],
) -> Dict[str, Any]:
    """取消一个还在排队的 wylon 任务。只在用户明确要求取消时调用。"""
    try:
        return await anyio.to_thread.run_sync(core.cancel_task, task_id)
    except core.WylonError as exc:
        raise _fail(exc)


@mcp.tool(annotations=ToolAnnotations(title="wylon 最近任务", readOnlyHint=True, openWorldHint=False))
def list_recent_tasks(
    limit: Annotated[int, Field(description="返回最近多少条，默认 10。", ge=1, le=100)] = 10,
) -> Dict[str, Any]:
    """列出本机最近提交过的 wylon 任务（task_id、类型、模型、提示词开头），用于找回中断的任务。"""
    return {"tasks": core.recent_tasks(limit), "ledger_file": str(core.TASK_LEDGER_FILE)}


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

USAGE = f"""wylon-mcp {__version__}

用法:
  wylon-mcp            启动 MCP 服务器（stdio），供 AI 客户端调用
  wylon-mcp setup      在本机保存 wylon API Key（输入不显示，存到 ~/.wylon/config.json）
  wylon-mcp check      检查 API Key 是否已配置
"""


def _setup() -> int:
    print(f"请到 {core.DASHBOARD_URL} 创建并复制 API Key。")
    key = getpass.getpass("粘贴 wylon API Key（输入不会显示）: ").strip()
    try:
        core.save_api_key(key)
    except core.WylonError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    print(f"已保存到 {core.CONFIG_FILE}（{core.mask_key(key)}）。重启 AI 客户端后生效。")
    return 0


def _check() -> int:
    try:
        key, base_url, source = core.resolve_credentials()
    except core.WylonError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    if not key:
        print(f"未配置 API Key。运行 `wylon-mcp setup`，或在 MCP 配置里设置 WYLON_API_KEY。")
        return 1
    print(f"已配置: {core.mask_key(key)}（来源 {source}），API: {base_url}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        mcp.run("stdio")
        return 0
    cmd = args[0]
    if cmd == "setup":
        return _setup()
    if cmd == "check":
        return _check()
    if cmd in ("--version", "-V"):
        print(__version__)
        return 0
    print(USAGE)
    return 0 if cmd in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    raise SystemExit(main())
