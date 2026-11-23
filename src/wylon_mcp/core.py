"""wylon API client: credentials, media encoding, task submit / poll / download.

Ported from the wylon-media skill script (scripts/wylon_media.py); kept dependency-free
so the same logic serves both the MCP server and any future CLI.
"""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__

USER_AGENT = f"wylon-mcp/{__version__}"

DEFAULT_BASE_URL = "https://api.wylon.cn/v1"
DASHBOARD_URL = "https://dashboard.wylon.cn"
PRICES_URL = "https://wylon.cn/models.json"
SETUP_COMMAND = "uvx --from git+https://github.com/blackwellb300/wylon-mcp wylon-mcp setup"
# API Key 只会发往这些域名；私有化部署需由用户显式设置 WYLON_ALLOW_CUSTOM_BASE_URL=1
TRUSTED_API_HOST_SUFFIXES = ("wylon.cn",)
TASKS_PATH = "/contents/generations/tasks"
CONFIG_DIR = Path.home() / ".wylon"
CONFIG_FILE = CONFIG_DIR / "config.json"
TASK_LEDGER_FILE = CONFIG_DIR / "tasks.jsonl"
DEFAULT_OUTPUT_DIR = Path.home() / "wylon_outputs"

MEBIBYTE = 1024 * 1024
MAX_IMAGE_BYTES = 30 * MEBIBYTE
MAX_AUDIO_BYTES = 15 * MEBIBYTE
MAX_IMAGE_DOWNLOAD_BYTES = 50 * MEBIBYTE
MAX_VIDEO_DOWNLOAD_BYTES = 150 * MEBIBYTE

IMAGE_SIZES: Dict[str, List[str]] = {
    "Qwen/Qwen-Image-2.1": [
        "2048x2048", "2752x1536", "1536x2752", "2400x1792",
        "1792x2400", "2528x1696", "1696x2528",
    ],
    "Qwen/Qwen-Image-2512": ["1328x1328", "1664x928", "928x1664", "1104x1472", "1584x1056"],
}
DEFAULT_IMAGE_MODEL = "Qwen/Qwen-Image-2.1"
EDIT_MODEL = "Qwen/Qwen-Image-2.1"  # the only wylon model documented for image editing
MAX_EDIT_IMAGES = 10
MAX_EDIT_IMAGE_BYTES = 30_000_000
MAX_EDIT_IMAGE_PIXELS = 33_177_600
MAX_EDIT_URL_CHARS = 20_971_520

H3_MODEL = "MiniMaxAI/MiniMax-H3"
REF2VA_MODEL = "MiniMaxAI/MiniMax-H3-Ref2VA"
VIDEO_MODELS = [H3_MODEL, REF2VA_MODEL]
VIDEO_RATIOS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]

IMAGE_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff", ".gif": "image/gif",
    ".heic": "image/heic", ".heif": "image/heif",
}
AUDIO_EXT_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MIN_IMAGE_SIDE, MAX_IMAGE_SIDE = 300, 5760
MIN_IMAGE_RATIO, MAX_IMAGE_RATIO = 0.4, 2.5

ProgressFn = Callable[[str], None]


class WylonError(Exception):
    """Error the model can act on: message + optional task_id and fix-it hint."""

    def __init__(self, message: str, task_id: Optional[str] = None, hint: Optional[str] = None):
        super().__init__(message)
        self.task_id = task_id
        self.hint = hint

    def render(self) -> str:
        parts = [str(self)]
        if self.task_id:
            parts.append(f"task_id: {self.task_id}")
        if self.hint:
            parts.append(f"建议: {self.hint}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def redact_text(text: str, api_key: str = "") -> str:
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(r"data:[^\s\"'<>]+", "[media redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"Bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    return text[:1800]


def mask_key(key: str) -> str:
    key = key.strip()
    return "****" if len(key) <= 8 else f"{key[:5]}...{key[-4:]}"


def load_config() -> Dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_api_key(api_key: str) -> None:
    key = api_key.strip()
    if not key:
        raise WylonError("API Key 不能为空。")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg["api_key"] = key
    cfg.setdefault("base_url", DEFAULT_BASE_URL)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def get_prompt_enhance() -> Optional[bool]:
    """User preference shared with the wylon-media skill: True expand, False verbatim, None not asked yet."""
    value = load_config().get("prompt_enhance")
    return value if isinstance(value, bool) else None


def set_prompt_enhance(enabled: Optional[bool]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if enabled is None:
        cfg.pop("prompt_enhance", None)
    else:
        cfg["prompt_enhance"] = enabled
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def validate_base_url(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise WylonError(f"Base URL 必须是不含凭据的 HTTPS 地址: {url}")
    trusted = any(host == s or host.endswith("." + s) for s in TRUSTED_API_HOST_SUFFIXES)
    if not trusted and os.environ.get("WYLON_ALLOW_CUSTOM_BASE_URL") != "1":
        raise WylonError(
            f"出于密钥安全考虑，默认只允许向 wylon 官方域名发送 API Key，当前域名: {host}",
            hint="如确需私有化部署地址，请在 MCP 配置的 env 中设置 WYLON_ALLOW_CUSTOM_BASE_URL=1。",
        )
    return url


def resolve_credentials() -> Tuple[str, str, str]:
    """Return (api_key, base_url, source)."""
    cfg = load_config()
    base_url = validate_base_url(
        os.environ.get("WYLON_BASE_URL") or cfg.get("base_url") or DEFAULT_BASE_URL
    )
    env_key = os.environ.get("WYLON_API_KEY", "").strip()
    if env_key:
        return env_key, base_url, "env:WYLON_API_KEY"
    cfg_key = str(cfg.get("api_key", "")).strip()
    if cfg_key:
        return cfg_key, base_url, str(CONFIG_FILE)
    return "", base_url, "none"


def require_credentials() -> Tuple[str, str]:
    api_key, base_url, _ = resolve_credentials()
    if not api_key:
        raise WylonError(
            "未配置 wylon API Key。",
            hint=(
                f"请用户前往 {DASHBOARD_URL} 创建 API Key，然后在 MCP 配置的 env 里加上 "
                f"WYLON_API_KEY，或在终端运行 `{SETUP_COMMAND}` 保存到本机，之后重启 AI 客户端。"
                "要用普通 API 密钥；Token 计划的订阅密钥目前只列了对话模型。"
                "不要让用户把 Key 发在对话里。"
            ),
        )
    return api_key, base_url


# ---------------------------------------------------------------------------
# Media inputs
# ---------------------------------------------------------------------------


def validate_public_https_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password or not host or "." not in host:
        raise WylonError(f"媒体链接必须为公网 HTTPS URL，且不能包含本地地址或凭据: {url}")
    if re.search(r"(?:^|\.)(localhost|local|internal|test|invalid)$", host):
        raise WylonError(f"不允许使用内网或本地域名作为媒体 URL: {host}")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise WylonError(f"不允许使用内网/保留 IP 地址作为媒体 URL: {host}")
    except ValueError:
        pass
    return url


def read_image_dimensions(raw: bytes) -> Optional[Tuple[int, int]]:
    """Best-effort (width, height) from PNG/JPEG/GIF/WebP/BMP headers."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            return struct.unpack(">II", raw[16:24])
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", raw[6:10])
        if raw[:2] == b"BM":
            w, h = struct.unpack("<ii", raw[18:26])
            return abs(w), abs(h)
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            chunk = raw[12:16]
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", raw[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = raw[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
            if chunk == b"VP8X":
                return 1 + int.from_bytes(raw[24:27], "little"), 1 + int.from_bytes(raw[27:30], "little")
        if raw[:2] == b"\xff\xd8":
            i, n = 2, len(raw)
            while i + 9 < n:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = struct.unpack(">H", raw[i + 2: i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5: i + 9])
                    return w, h
                i += 2 + seg_len
    except (struct.error, IndexError):
        return None
    return None


def check_image_dimensions(raw: bytes, path: Path) -> None:
    dims = read_image_dimensions(raw)
    if not dims or dims[0] <= 0 or dims[1] <= 0:
        return  # HEIC/TIFF 等交给服务端校验
    w, h = dims
    if not (MIN_IMAGE_SIDE <= w <= MAX_IMAGE_SIDE and MIN_IMAGE_SIDE <= h <= MAX_IMAGE_SIDE):
        raise WylonError(
            f"图片尺寸 {w}x{h} 超出允许范围（宽高均需在 {MIN_IMAGE_SIDE}~{MAX_IMAGE_SIDE}px）: {path}",
            hint="请先缩放或裁剪图片再提交。",
        )
    ratio = w / h
    if not (MIN_IMAGE_RATIO <= ratio <= MAX_IMAGE_RATIO):
        raise WylonError(
            f"图片宽高比 {ratio:.2f} 超出允许范围（{MIN_IMAGE_RATIO}~{MAX_IMAGE_RATIO}）: {path}",
            hint="请先裁剪图片再提交。",
        )


def encode_media_input(value: str, kind: str) -> str:
    """Public HTTPS URL is passed through; a local file becomes a data URI."""
    val = value.strip()
    if val.startswith(("https://", "http://")):
        return validate_public_https_url(val)
    if kind == "video":
        raise WylonError(
            f"参考视频不支持本地文件，只支持公网 HTTPS 链接（MP4/MOV）: {val}",
            hint="请先把视频上传到可公开访问的 HTTPS 地址再传入。",
        )
    path = Path(val).expanduser().resolve()
    if not path.is_file():
        raise WylonError(f"找不到本地{'图片' if kind == 'image' else '音频'}文件: {path}",
                         hint="请传入绝对路径。")
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_AUDIO_BYTES
    size = path.stat().st_size
    if size <= 0 or size > limit:
        raise WylonError(f"文件体积不合法（{size} 字节，上限 {limit // MEBIBYTE}MB）: {path}")
    mime_map = IMAGE_EXT_MIME if kind == "image" else AUDIO_EXT_MIME
    mime = mime_map.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or ""
    allowed = (r"^image/(jpeg|png|webp|bmp|tiff|gif|heic|heif)$" if kind == "image"
               else r"^audio/(wav|x-wav|wave|mpeg|mp3)$")
    if not re.match(allowed, mime):
        raise WylonError(f"不支持的格式 ({mime or path.suffix}): {path}")
    raw = path.read_bytes()
    if kind == "image":
        check_image_dimensions(raw, path)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

HTTP_HINTS = {
    400: "请求参数不被接受，请对照 list_models 检查模型、尺寸、比例、时长和素材后修正重试。",
    401: "API Key 无效或已失效，请让用户到 https://dashboard.wylon.cn 检查 Key 后更新配置。",
    402: "账户余额不足，请让用户充值后再试，不要自动重试。",
    403: "该 Key 没有权限调用此模型。请让用户在控制台检查权限；如果用的是 Token 计划的订阅密钥，"
         "请换成普通 API 密钥（套餐目前只列了对话模型）。不要自动重试。",
    429: "请求过于频繁或额度受限，请稍后再试，不要连续自动重试。",
    503: "服务暂时不可用，任务没有创建，可以稍后再试。",
}
CODE_HINTS = {
    "AccountOverdueError": HTTP_HINTS[402],
    "insufficient_balance": HTTP_HINTS[402],
    "NotFound.Task": "任务不存在、已取消或记录已删除，请核对 task_id（可用 list_recent_tasks 查看）。",
    "TASK_RUNNING": "任务已经开始运行，不能取消，只能等它结束。",
}
# After a POST with no clear answer the task may exist and be billed; it can only be confirmed in the console.
UNCERTAIN_SUBMIT_HINT = (
    "没有收到明确的提交结果，任务可能已经创建并计费。不要自动重新提交：请让用户先到 "
    "https://dashboard.wylon.cn 查看任务记录和用量，确认没有创建后，征得用户同意再提交。"
)
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError)  # OSError covers timeouts / resets


def _error_code(body: str) -> str:
    try:
        err = json.loads(body).get("error")
        return str(err.get("code") or "") if isinstance(err, dict) else ""
    except (ValueError, AttributeError):
        return ""


def api_request(method: str, base_url: str, path: str, api_key: str,
                payload: Optional[Dict[str, Any]] = None, timeout: float = 120.0) -> Dict[str, Any]:
    url = base_url + path
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT}
    data: Optional[bytes] = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    uncertain = UNCERTAIN_SUBMIT_HINT if method == "POST" else None
    max_attempts = 3 if method == "GET" else 1
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except NETWORK_ERRORS:
                body = ""
            if method == "GET" and exc.code in (429, 500, 502, 503, 504) and attempt + 1 < max_attempts:
                time.sleep(0.5 * (2 ** attempt))
                continue
            hint = CODE_HINTS.get(_error_code(body)) or HTTP_HINTS.get(exc.code)
            if exc.code >= 500 and exc.code != 503:
                hint = uncertain or hint
            raise WylonError(f"wylon API 返回 HTTP {exc.code}: {redact_text(body, api_key)}", hint=hint)
        except NETWORK_ERRORS as exc:
            if method == "GET" and attempt + 1 < max_attempts:
                time.sleep(0.5 * (2 ** attempt))
                continue
            reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
            # DNS failure / refused connection happen before anything is sent, so nothing was created.
            not_sent = isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError))
            raise WylonError(
                f"网络请求失败: {redact_text(str(reason), api_key)}",
                hint="请求没有发出，没有产生任务和费用，请检查网络后再试。" if not_sent and uncertain
                else uncertain or "请检查网络后再试。")
        if method == "DELETE":
            return {}  # wylon defines no response body for DELETE; the 2xx status is the result
        if not raw:
            parsed: Any = {}
        else:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise WylonError("wylon API 返回了无法解析的 JSON。", hint=uncertain)
        if not isinstance(parsed, dict):
            raise WylonError("wylon API 返回了非 JSON 对象响应。", hint=uncertain)
        return parsed
    raise WylonError("请求重试失败。")


def download_binary(url: str, max_bytes: int, dest: Path, task_id: Optional[str],
                    timeout: float = 180.0) -> Path:
    validate_public_https_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    retry_hint = "用 get_task 和同一个 task_id 重新下载即可，不要重新生成。"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                length = resp.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise WylonError(f"结果文件超过大小限制 ({max_bytes // MEBIBYTE}MB)", task_id=task_id)
                chunks: List[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise WylonError(f"结果文件超过大小限制 ({max_bytes // MEBIBYTE}MB)", task_id=task_id)
                    chunks.append(chunk)
                if total == 0:
                    raise WylonError("下载到的结果文件为空。", task_id=task_id, hint=retry_hint)
                tmp = dest.with_suffix(dest.suffix + ".part")
                tmp.write_bytes(b"".join(chunks))
                tmp.replace(dest)
                return dest
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise WylonError(f"结果文件下载失败 HTTP {exc.code}", task_id=task_id, hint=retry_hint)
        except NETWORK_ERRORS as exc:
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue
            reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
            raise WylonError(f"结果文件下载失败: {reason}", task_id=task_id, hint=retry_hint)
    raise WylonError("下载重试失败。", task_id=task_id, hint=retry_hint)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def output_dir(explicit: Optional[str]) -> Path:
    raw = explicit or os.environ.get("WYLON_OUTPUT_DIR")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_OUTPUT_DIR


def output_path(out_dir: Optional[str], kind: str, task_id: str) -> Path:
    ext = ".png" if kind == "image" else ".mp4"
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir(out_dir) / f"wylon_{kind}_{ts}_{task_id[:12]}{ext}"


def find_existing_output(out_dir: Optional[str], kind: str, task_id: str) -> Optional[Path]:
    """A previous call may already have saved this task; never download twice."""
    d = output_dir(out_dir)
    ext = ".png" if kind == "image" else ".mp4"
    if not d.is_dir():
        return None
    hits = sorted(d.glob(f"wylon_{kind}_*_{task_id[:12]}{ext}"))
    return hits[-1] if hits else None


def record_task(task_id: str, kind: str, model: str, prompt: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "task_id": task_id, "kind": kind, "model": model, "prompt": prompt[:160],
            "source": "mcp",
        }
        with TASK_LEDGER_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_tasks(limit: int) -> List[Dict[str, Any]]:
    try:
        lines = TASK_LEDGER_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    items.reverse()
    return items


def validate_task_id(task_id: str) -> str:
    tid = task_id.strip()
    if not TASK_ID_RE.match(tid):
        raise WylonError(f"无效的 task_id: {task_id}")
    return tid


def submit_task(payload: Dict[str, Any], kind: str, prompt: str) -> str:
    api_key, base_url = require_credentials()
    payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    try:
        created = api_request("POST", base_url, TASKS_PATH, api_key, payload=payload)
    except WylonError as exc:
        if "HTTP 404" in str(exc) and "model" in str(exc).lower():
            exc.hint = unavailable_hint(kind)
        raise
    try:
        tid = validate_task_id(str(created.get("id", "")))
    except WylonError:
        raise WylonError("wylon API 没有返回有效的任务 ID。", hint=UNCERTAIN_SUBMIT_HINT)
    record_task(tid, kind, str(payload.get("model")), prompt)
    return tid


def _save_result(result: Dict[str, Any], kind: str, out_dir: Optional[str], tid: str) -> Dict[str, Any]:
    dest = output_path(out_dir, kind, tid)
    if kind == "image":
        data = result.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise WylonError("任务完成但未返回图片数据。", task_id=tid)
        item = data[0]
        if isinstance(item.get("b64_json"), str):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(item["b64_json"]))
            return {"local_path": str(dest)}
        url = item.get("url")
        max_bytes = MAX_IMAGE_DOWNLOAD_BYTES
    else:
        content = result.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("video_url"), str):
            raise WylonError("视频任务完成但未返回 video_url。", task_id=tid)
        url = content["video_url"]
        max_bytes = MAX_VIDEO_DOWNLOAD_BYTES
    if not isinstance(url, str):
        raise WylonError("任务完成但缺少可下载的结果链接。", task_id=tid)
    download_binary(url, max_bytes, dest, tid)
    # the signed URL expires within minutes and is long; the local file is what matters
    return {"local_path": str(dest), "bytes": dest.stat().st_size}


RETENTION_NOTE = "结果生成后云端只保留 2 小时，请及时用 get_task 取回。"


def result_info(result: Dict[str, Any]) -> Dict[str, Any]:
    """What was actually produced, as reported by the API."""
    info: Dict[str, Any] = {}
    if result.get("width") and result.get("height"):
        info["actual_size"] = f"{result['width']}x{result['height']}"
    for key in ("duration", "ratio", "seed"):
        if result.get(key) is not None:
            info[key] = result[key]
    return info


def poll_task(task_id: str, expected_kind: Optional[str], wait_seconds: float,
              out_dir: Optional[str], progress: Optional[ProgressFn] = None) -> Dict[str, Any]:
    """Poll until done or wait_seconds elapses; download the result on success."""
    api_key, base_url = require_credentials()
    tid = validate_task_id(task_id)
    deadline = time.time() + max(0.0, wait_seconds)
    attempt = 0
    while True:
        result = api_request("GET", base_url,
                             f"{TASKS_PATH}/{urllib.parse.quote(tid, safe='')}?detail=true", api_key)
        status = str(result.get("status", "unknown"))
        file_status = str(result.get("file_status", ""))
        kind = str(result.get("type") or expected_kind or "video")
        model = result.get("model")

        if status == "succeeded" and file_status in ("available", "expired"):
            existing = find_existing_output(out_dir, kind, tid)
            if existing:
                return {"status": "succeeded", "kind": kind, "task_id": tid, "model": model,
                        "local_path": str(existing), "note": "该任务结果此前已保存到本地。", **result_info(result)}
            if file_status == "expired":
                raise WylonError(
                    "任务已成功，但云端结果文件已过期（云端只保留 2 小时），无法再下载。",
                    task_id=tid,
                    hint="如果之前下载过，请在输出目录里按 task_id 查找；否则只能重新生成，这会再次计费，需先征得用户同意。",
                )
            saved = _save_result(result, kind, out_dir, tid)
            return {"status": "succeeded", "kind": kind, "task_id": tid, "model": model, **saved,
                    **result_info(result)}

        if status in ("failed", "cancelled", "expired") or file_status == "expired":
            detail = result.get("error") or f"status={status}, file_status={file_status or 'unknown'}"
            raise WylonError(f"任务没有成功: {redact_text(str(detail), api_key)}", task_id=tid)

        remaining = deadline - time.time()
        if remaining <= 0:
            return {
                "status": "pending", "kind": kind, "task_id": tid, "model": model,
                "task_status": status, "file_status": file_status or None,
                "result_retention": RETENTION_NOTE,
                "message": "任务仍在云端处理中，已提交成功，不要重新提交。"
                           "请稍后再次调用 get_task 并传入同一个 task_id。",
            }
        if progress:
            progress(f"任务 {tid} 状态 {status}，继续等待……")
        time.sleep(min(2.0 * (1.4 ** attempt), 8.0, remaining))
        attempt += 1


def cancel_task(task_id: str) -> Dict[str, Any]:
    """Cancel a queued task only. DELETE on a finished task would erase its record (and an undownloaded result)."""
    api_key, base_url = require_credentials()
    tid = validate_task_id(task_id)
    path = f"{TASKS_PATH}/{urllib.parse.quote(tid, safe='')}"
    status = str(api_request("GET", base_url, path, api_key).get("status", "unknown"))
    if status != "queued":
        reason = {
            "running": "任务已经开始运行，不能取消，只能等它结束。",
            "succeeded": "任务已经完成，没有取消。用 get_task 下载结果。",
        }.get(status, f"任务已经结束（{status}），不需要取消。")
        return {"status": "not_cancelled", "task_id": tid, "task_status": status, "message": reason}
    api_request("DELETE", base_url, path, api_key)
    return {"status": "cancelled", "task_id": tid,
            "message": "排队中的任务已取消，任务记录也已删除。是否产生费用以控制台账单为准。"}


# ---------------------------------------------------------------------------
# Live model availability (wylon may take models on/offline)
# ---------------------------------------------------------------------------

_LIVE_CACHE: Dict[str, Any] = {"at": 0.0, "models": None}
LIVE_TTL = 300.0


def live_models() -> Optional[Dict[str, str]]:
    """{model_id: type} from GET /models, cached; None if it could not be fetched."""
    if _LIVE_CACHE["models"] is not None and time.time() - _LIVE_CACHE["at"] < LIVE_TTL:
        return _LIVE_CACHE["models"]
    try:
        api_key, base_url = require_credentials()
        res = api_request("GET", base_url, "/models", api_key, timeout=20)
    except WylonError:
        return None
    models = {str(m.get("id")): str(m.get("type", "")) for m in res.get("data", []) if isinstance(m, dict)}
    _LIVE_CACHE.update(at=time.time(), models=models)
    return models


def is_available(model: str) -> Optional[bool]:
    live = live_models()
    return None if live is None else model in live


def pick_image_model(requested: Optional[str]) -> str:
    """Explicit choice wins; otherwise the default, falling back to whatever image model is online."""
    if requested:
        return requested
    live = live_models()
    if live is None or DEFAULT_IMAGE_MODEL in live:
        return DEFAULT_IMAGE_MODEL
    for mid in IMAGE_SIZES:
        if mid in live:
            return mid
    return DEFAULT_IMAGE_MODEL


def _ratio(size: str) -> float:
    w, h = size.lower().split("x")
    return int(w) / int(h)


def closest_size(model: str, size: str) -> str:
    target = _ratio(size)
    return min(IMAGE_SIZES[model], key=lambda s: abs(_ratio(s) - target))


def unavailable_hint(kind: str) -> str:
    live = live_models() or {}
    known = IMAGE_SIZES if kind == "image" else VIDEO_MODELS
    online = [m for m in known if m in live]
    if online:
        return f"该模型当前未上线或你的账户无权使用。当前可用的{'生图' if kind == 'image' else '生视频'}模型: {', '.join(online)}。"
    return "当前没有可用的模型，请让用户在 https://dashboard.wylon.cn 检查账户权限。"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def build_image_payload(prompt: str, requested_model: Optional[str], size: Optional[str],
                        seed: Optional[int]) -> Dict[str, Any]:
    """Returns the payload; payload["_notes"] (removed before sending) explains any auto-adjustment."""
    model = pick_image_model(requested_model)
    notes: List[str] = []
    if model not in IMAGE_SIZES:
        raise WylonError(f"不支持的生图模型: {model}", hint=f"可选模型: {', '.join(IMAGE_SIZES)}")
    if requested_model is None and model != DEFAULT_IMAGE_MODEL:
        notes.append(f"默认模型 {DEFAULT_IMAGE_MODEL} 当前不可用，已自动改用 {model}。")
    prompt = prompt.strip()
    if not prompt:
        raise WylonError("prompt 不能为空。")
    if size and size not in IMAGE_SIZES[model]:
        if requested_model is None and re.match(r"^\d+x\d+$", size):
            adjusted = closest_size(model, size)
            notes.append(f"{model} 不支持 {size}，已按最接近的宽高比改用 {adjusted}。")
            size = adjusted
        else:
            raise WylonError(f"模型 {model} 不支持尺寸 {size}",
                             hint=f"该模型可用尺寸: {', '.join(IMAGE_SIZES[model])}；也可以不传 size 用默认尺寸。")
    payload: Dict[str, Any] = {"type": "image", "model": model, "prompt": prompt, "n": 1,
                               "output_format": "png"}
    if size:
        payload["size"] = size
    if seed is not None and seed >= 0:
        payload["seed"] = seed
    if notes:
        payload["_notes"] = notes
    return payload


def encode_edit_image(value: str) -> str:
    """Reference image for editing: limits differ from video references (no HEIC/BMP/TIFF, no min side or ratio)."""
    val = value.strip()
    if val.startswith(("https://", "http://")):
        return validate_public_https_url(val)
    path = Path(val).expanduser().resolve()
    if not path.is_file():
        raise WylonError(f"找不到本地图片文件: {path}", hint="请传入绝对路径。")
    size = path.stat().st_size
    if size <= 0 or size > MAX_EDIT_IMAGE_BYTES:
        raise WylonError(f"图片体积不合法（{size} 字节，上限 30MB）: {path}")
    raw = path.read_bytes()
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:2] == b"\xff\xd8":
        mime = "image/jpeg"
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise WylonError(f"图像编辑只支持 PNG、JPEG、WebP、GIF: {path}", hint="请先转换格式，例如把 HEIC 导出为 JPEG。")
    dims = read_image_dimensions(raw)
    if dims and dims[0] * dims[1] > MAX_EDIT_IMAGE_PIXELS:
        raise WylonError(f"图片 {dims[0]}x{dims[1]} 总像素超出上限（{MAX_EDIT_IMAGE_PIXELS}）: {path}",
                         hint="请先缩小图片再提交。")
    url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    if len(url) > MAX_EDIT_URL_CHARS:
        raise WylonError(f"本地图片太大（编码后超过 20MB）: {path}",
                         hint="请压缩到约 15MB 以内，或改用公网 HTTPS 链接。")
    return url


def build_edit_payload(prompt: str, images: List[str], size: Optional[str], seed: Optional[int]) -> Dict[str, Any]:
    prompt = prompt.strip()
    if not prompt:
        raise WylonError("prompt 不能为空。")
    if not 1 <= len(images) <= MAX_EDIT_IMAGES:
        raise WylonError(f"图像编辑需要 1~{MAX_EDIT_IMAGES} 张图片，当前 {len(images)} 张。")
    size = size or "auto"
    if size != "auto" and size not in IMAGE_SIZES[EDIT_MODEL]:
        raise WylonError(f"图像编辑不支持尺寸 {size}",
                         hint=f"可用尺寸: auto, {', '.join(IMAGE_SIZES[EDIT_MODEL])}。")
    encoded = [{"image_url": encode_edit_image(i)} for i in images]
    if is_available(EDIT_MODEL) is False:
        raise WylonError(f"图像编辑模型 {EDIT_MODEL} 当前未上线或你的账户无权使用，没有提交、没有扣费。",
                         hint="请稍后再试，或在 https://dashboard.wylon.cn 检查账户权限。其他生图模型不支持图像编辑。")
    payload: Dict[str, Any] = {"type": "image", "model": EDIT_MODEL, "prompt": prompt, "images": encoded,
                               "size": size, "n": 1, "output_format": "png"}
    if seed is not None:
        payload["seed"] = seed
    return payload


def build_video_payload(prompt: str, model: str, duration: int, ratio: Optional[str],
                        first_frame: Optional[str], last_frame: Optional[str],
                        ref_images: List[str], ref_videos: List[str], ref_audios: List[str],
                        negative_prompt: Optional[str], seed: Optional[int]) -> Dict[str, Any]:
    if model not in VIDEO_MODELS:
        raise WylonError(f"不支持的生视频模型: {model}", hint=f"可选模型: {', '.join(VIDEO_MODELS)}")
    prompt = prompt.strip()
    if not prompt:
        raise WylonError("prompt 不能为空。")
    if not 5 <= duration <= 15:
        raise WylonError("duration 必须是 5~15 之间的整数秒。")

    has_frames = bool(first_frame or last_frame)
    has_refs = bool(ref_images or ref_videos or ref_audios)
    if model == H3_MODEL and has_refs:
        raise WylonError(f"{H3_MODEL} 只支持 first_frame / last_frame，不支持多参考素材。",
                         hint=f"需要 ref_images / ref_videos / ref_audios 时请改用 {REF2VA_MODEL}。")
    if model == REF2VA_MODEL:
        if has_frames:
            raise WylonError(f"{REF2VA_MODEL} 不能使用 first_frame / last_frame，请改用 ref_images。")
        if not ref_images and not ref_videos:
            raise WylonError(f"{REF2VA_MODEL} 至少需要 1 张参考图或 1 个参考视频（不支持只有音频）。")
        if len(ref_images) > 9 or len(ref_videos) > 3 or len(ref_audios) > 3:
            raise WylonError(f"{REF2VA_MODEL} 素材上限：9 张图、3 个视频链接、3 段音频。")
    if ratio is None:  # wylon's own default: follow the image when a frame is given
        ratio = "adaptive" if model == H3_MODEL and has_frames else "16:9"
    if ratio not in VIDEO_RATIOS and not (ratio == "adaptive" and model == H3_MODEL and has_frames):
        raise WylonError(f"不支持的视频比例: {ratio}",
                         hint=f"可选比例: {', '.join(VIDEO_RATIOS)}；{H3_MODEL} 传了首/尾帧时还可用 adaptive。")

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if first_frame:
        content.append({"type": "image_url", "image_url": {"url": encode_media_input(first_frame, "image")},
                        "role": "first_frame"})
    if last_frame:
        content.append({"type": "image_url", "image_url": {"url": encode_media_input(last_frame, "image")},
                        "role": "last_frame"})
    for img in ref_images:
        content.append({"type": "image_url", "image_url": {"url": encode_media_input(img, "image")},
                        "role": "reference_image"})
    for vid in ref_videos:
        content.append({"type": "video_url", "video_url": {"url": encode_media_input(vid, "video")},
                        "role": "reference_video"})
    for aud in ref_audios:
        content.append({"type": "audio_url", "audio_url": {"url": encode_media_input(aud, "audio")},
                        "role": "reference_audio"})

    payload: Dict[str, Any] = {"type": "video", "model": model, "content": content,
                               "duration": duration, "resolution": "768p"}
    if ratio != "adaptive":  # omitted = adaptive when a frame is given
        payload["ratio"] = ratio
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if seed is not None and seed >= 0:
        payload["seed"] = seed
    return payload


# List prices in CNY, snapshot of PRICES_URL on 2026-11-23; used when the live file cannot be read.
PRICE_SNAPSHOT: Dict[str, Dict[str, float]] = {
    "image": {"image": 0.25},
    "video": {"input_image": 0.2, "input_video": 0.5, "input_audio": 0.0, "output_video": 0.5},
}
_PRICE_CACHE: Dict[str, Any] = {"at": 0.0, "prices": None}


def _parse_prices(doc: Any) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for m in doc["models"]:
        p = m["pricing"][0] if m.get("pricing") else {}
        if m.get("name") == "Qwen-Image-2.1" and "price" in p:
            out["image"] = {"image": float(p["price"])}
        elif m.get("name") == "MiniMax H3" and "rates" in p:
            out["video"] = {k: float(v) for k, v in p["rates"].items()}
    if set(out) != {"image", "video"} or not set(PRICE_SNAPSHOT["video"]) <= set(out["video"]):
        raise ValueError("unexpected price format")
    return out


def list_prices() -> Tuple[Dict[str, Dict[str, float]], bool]:
    """(prices, is_live). Public file, no API key is sent."""
    if _PRICE_CACHE["prices"] is not None and time.time() - _PRICE_CACHE["at"] < 3600:
        return _PRICE_CACHE["prices"]
    try:
        req = urllib.request.Request(PRICES_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = (_parse_prices(json.loads(resp.read(1_000_000).decode("utf-8"))), True)
    except (*NETWORK_ERRORS, ValueError, KeyError, TypeError, IndexError, AttributeError):
        result = (PRICE_SNAPSHOT, False)
    _PRICE_CACHE.update(at=time.time(), prices=result)
    return result


PRICE_NOTE = "按官网刊例价估算，实际以 wylon 控制台账单为准。"


def price_catalog() -> Dict[str, Any]:
    prices, live = list_prices()
    v = prices["video"]
    return {
        "source": PRICES_URL if live else "内置价格（2026-11-23），未能读取最新价格",
        "note": PRICE_NOTE,
        "image": f"Qwen-Image-2.1 生图和编辑：¥{prices['image']['image']:.2f}/张；其他生图模型官网未列价",
        "video": f"MiniMax-H3 / Ref2VA：输出 ¥{v['output_video']:.2f}/秒；首尾帧或参考图 ¥{v['input_image']:.2f}/张；"
                 f"参考视频 ¥{v['input_video']:.2f}/秒；参考音频 "
                 + ("免费" if not v["input_audio"] else f"¥{v['input_audio']:.2f}/秒"),
    }


def estimate_cost(payload: Dict[str, Any]) -> Dict[str, Any]:
    # never blocks on the network: live prices if already loaded, otherwise the snapshot
    prices = (_PRICE_CACHE["prices"] or (PRICE_SNAPSHOT, False))[0]
    if payload["type"] == "image":
        if payload["model"] != DEFAULT_IMAGE_MODEL:
            return {"estimated_cost": "官网未列该模型价格，以 wylon 控制台为准"}
        return {"estimated_cost": f"¥{prices['image']['image']:.2f}", "cost_note": PRICE_NOTE}
    v = prices["video"]
    types = [c["type"] for c in payload["content"]]
    total = payload["duration"] * v["output_video"] + types.count("image_url") * v["input_image"]
    text = f"¥{total:.2f}"
    if "video_url" in types:
        text += f"，另加参考视频 ¥{v['input_video']:.2f}/秒"
    if "audio_url" in types and v["input_audio"]:
        text += f"，另加参考音频 ¥{v['input_audio']:.2f}/秒"
    return {"estimated_cost": text, "cost_note": PRICE_NOTE}


MODEL_CATALOG: Dict[str, Any] = {
    "image_models": [
        {
            "id": "Qwen/Qwen-Image-2.1", "default": True,
            "best_for": "默认生图模型，2K 级，适合海报、插画、商品图；也是唯一支持图像编辑（edit_image）的模型",
            "sizes": {
                "2048x2048": "1:1（默认）", "2752x1536": "≈16:9 横版", "1536x2752": "≈9:16 竖版/手机",
                "2400x1792": "4:3", "1792x2400": "3:4 小红书/竖版海报",
                "2528x1696": "3:2", "1696x2528": "2:3",
            },
        },
        {
            "id": "Qwen/Qwen-Image-2512", "default": False,
            "best_for": "备选，用户点名时使用，最大 1664px；不支持图像编辑，官网 API 手册和价格表目前未列出",
            "sizes": {"1328x1328": "1:1", "1664x928": "16:9", "928x1664": "9:16",
                      "1104x1472": "3:4", "1584x1056": "3:2"},
        },
    ],
    "video_models": [
        {
            "id": H3_MODEL, "default": True,
            "best_for": "文生视频、首帧图生视频、首尾帧过渡；自带立体声音效与人声",
            "resolution": "768p", "duration_seconds": "5~15，默认 5",
            "ratios": VIDEO_RATIOS + ["adaptive（仅传了首/尾帧时，也是此时的默认值）"],
            "inputs": "first_frame / last_frame：本地图片路径或公网 HTTPS 链接",
        },
        {
            "id": REF2VA_MODEL, "default": False,
            "best_for": "多参考保持人物/商品一致，音画联合控制",
            "resolution": "768p", "duration_seconds": "5~15，默认 5", "ratios": VIDEO_RATIOS,
            "inputs": "ref_images ≤9（本地或 HTTPS）、ref_videos ≤3（仅公网 HTTPS MP4/MOV）、"
                      "ref_audios ≤3（本地或 HTTPS 的 WAV/MP3）；至少 1 张图或 1 个视频，不能只有音频",
            "ref_video_limits": "每段 2~15 秒、合计 ≤15 秒、≤50MB；H.264/H.265；宽高 300~5760px，宽高比 0.4~2.5，帧率 23.976~60",
            "ref_audio_limits": "每段 2~15 秒、合计 ≤15 秒、≤15MB",
            "prompt_tags": "提示词里按传入顺序用 <Picture 1>、<Video 1>、<Audio 1> 引用素材",
        },
    ],
    "image_edit": "edit_image：1~10 张参考图（本地 PNG/JPEG/WebP/GIF 或公网 HTTPS），单张 ≤30MB（本地约 ≤15MB）、"
                  "总像素 ≤33177600；尺寸默认 auto（按第一张图的宽高比），不支持蒙版和透明背景",
    "image_input_limits": "视频的本地参考图单张 ≤30MB，宽高 300~5760px，宽高比 0.4~2.5",
}
