# wylon-mcp

wylon（https://wylon.cn）生图、生视频的 MCP 服务器。装好以后，WorkBuddy、Claude、Cursor、CodeBuddy、DeepSeek Harness 等 AI 工具可以直接用一句话生成图片和视频，结果自动保存到本机。

- 生图：Qwen-Image 系列，2K 级
- 生视频：MiniMax-H3（文生视频、首帧/首尾帧图生视频，带原生音效），MiniMax-H3-Ref2VA（多图/视频/音频参考，保持人物或商品一致）
- 生成费用由 wylon 按你的账户计费

## 一、准备

1. 在 https://dashboard.wylon.cn 创建一个 API Key。
2. 安装 uv（只装一次，它会自动准备 Python）：
   - macOS / Linux：`curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows（PowerShell）：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

## 二、配置 API Key（二选一）

**方式 A（推荐）：保存到本机**，所有 AI 工具共用，Key 不会写进任何配置文件：

```bash
uvx wylon-mcp setup
```

按提示粘贴 Key（输入不会显示），保存在 `~/.wylon/config.json`。用 `uvx wylon-mcp check` 可检查是否配置成功。

**方式 B：写在 AI 工具的 MCP 配置里**：在下面的配置中加上 `"env": {"WYLON_API_KEY": "你的Key"}`。

## 三、在 AI 工具里添加

大多数工具使用同一种 JSON 格式：

```json
{
  "mcpServers": {
    "wylon": {
      "command": "uvx",
      "args": ["wylon-mcp"]
    }
  }
}
```

| 工具 | 配置位置 |
|---|---|
| WorkBuddy | 在「连接器」里添加自定义 MCP，或编辑 `~/.workbuddy-ai/mcp.json`；然后打开 wylon 的开关 |
| Claude Desktop | 设置 → 开发者 → 编辑配置（`claude_desktop_config.json`） |
| Cursor | `~/.cursor/mcp.json` |
| Claude Code | 终端运行 `claude mcp add wylon -- uvx wylon-mcp` |
| DeepSeek Harness | 见下方 |

> **如果 AI 工具提示找不到 `uvx`**：桌面应用往往读不到终端里的 PATH。在终端运行 `which uvx`，把输出的完整路径（例如 `/Users/你/.local/bin/uvx` 或 `/opt/homebrew/bin/uvx`）填到 `"command"` 里，代替 `"uvx"`。

### DeepSeek Harness：作为插件安装

打开 **Plugins（插件）→ Add plugin（添加插件）**，填入本仓库的 Git 地址，或本文件夹的完整路径。需要先安装 uv 和 Git（填文件夹路径时不需要 Git）。API Key 用方式 A 配置。

如果以前在 `cordis.patch.yml` 里手动添加过 `serverName: wylon`，请先删掉那一段，否则两者重名，后加载的会失败。

### DeepSeek Harness：手动配置

不装插件也可以在 `~/.dsh/profiles/desktop/cordis.patch.yml` 末尾加：

```yaml
- id: mcp-wylon
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: wylon
    transport: stdio
    command: uvx
    args: ['wylon-mcp']
    toolCallTimeoutMs: 120000
```

DeepSeek Harness 不会把系统里名字带 KEY 的环境变量传给 MCP。请用方式 A，或在上面的 `config` 里写明 `env: { WYLON_API_KEY: 你的Key }`（写在配置里的会保留）。

添加后重启 AI 工具，然后试试：

- 「用 wylon 画一张秋季新品咖啡的竖版海报，顶部写“秋日限定”」
- 「用 wylon 生成一段 5 秒的航拍视频，清晨薄雾中的山谷，有风声和鸟鸣」
- 「用 wylon 把 /Users/me/Desktop/product.png 做成缓慢旋转的商品展示视频」

## 四、工具说明

| 工具 | 作用 | 计费 |
|---|---|---|
| `list_models` | 列出模型、尺寸、比例、时长，以及当前哪些模型在线 | 否 |
| `generate_image` | 生成一张图片，完成后保存到本地并附带预览图 | 是 |
| `create_video_task` | 提交视频任务，立即返回 task_id | 是 |
| `get_task` | 查询任务进度，完成后下载到本地；也用于找回中断的任务 | 否 |
| `cancel_task` | 取消排队中的任务 | 否 |
| `set_prompt_enhance` | 保存“是否扩写提示词”的偏好（on / off / ask） | 否 |
| `list_recent_tasks` | 列出本机最近提交过的任务 | 否 |

- 文件默认保存在 `~/wylon_outputs`，可以用环境变量 `WYLON_OUTPUT_DIR` 修改，也可以在对话里指定目录。
- 每次工具调用默认最多等待约 25 秒，以兼容超时较短的客户端（如 WorkBuddy 的 30 秒）。视频一般需要 1~5 分钟，AI 会多次调用 `get_task` 直到完成；中途断开也能凭 task_id 找回，不会重复扣费。
- 默认参数都按省钱的档位设置：一次一张图，视频 5 秒。
- 第一次生成前会先问你是否让 AI 扩写提示词（这一步不扣费），选择会被记住；想改时直接说“以后不要扩写”或“以后帮我扩写”。
- 默认生图模型暂时不可用时，会自动换成在线的模型，并按宽高比调整尺寸，结果里会附上说明。

## 五、隐私与安全

- 提示词和参考素材会发送到 wylon 云端用于生成。
- 本机只保存 `~/.wylon/config.json`（API Key，权限 600）和 `~/.wylon/tasks.jsonl`（task_id、模型、提示词前 160 字，用于找回任务）。
- API Key 只会发往 `*.wylon.cn`。需要使用私有化部署地址时，设置 `WYLON_BASE_URL`，同时设置 `WYLON_ALLOW_CUSTOM_BASE_URL=1`。
- 生成的图片和视频带有服务端添加的“AI 生成”标识。

## 开发

```bash
uv sync
uv run python tests/smoke_client.py offline   # 不计费：工具列表 + 参数校验
uv run python tests/smoke_client.py image     # 计费：真实生成一张图
uv run python tests/video_flow.py             # 计费：真实生成一条 5 秒视频
uv build                                      # 打包，发布用 uv publish
```
