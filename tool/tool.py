"""
Grok 插件共享工具模块

提供共用的参数、工具函数和共享逻辑。
"""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

# ─── 常量 ───────────────────────────────────────────────

# 默认系统提示词（要求返回 JSON 格式，LLM Tool 和 Skill 使用）
DEFAULT_JSON_SYSTEM_PROMPT = (
    "You are a web research assistant with real-time search capabilities. "
    "Search Strategy: 1) Approach from multiple angles, explore broadly first. "
    "2) Then dive deep into the most relevant findings. "
    "3) Prioritize authoritative sources (official docs, Wikipedia, academic papers, reputable media). "
    "4) Search in English first for breadth, then in Chinese if the query demands it. "
    "Return ONLY a single JSON object with keys: "
    "content (string, evidence-backed, concise), "
    "sources (array of objects with url/title/snippet, ordered by relevance). "
    "Every claim must be traceable to a source. "
    "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
)

# /grok 指令内置提示词：文本模式（直接发消息，QQ 等渠道不渲染 Markdown，要求纯文本）
CMD_TEXT_SYSTEM_PROMPT = (
    "You are a web research assistant. Use live web search/browsing when answering. "
    "Return ONLY a single JSON object with keys: "
    "content (string), sources (array of objects with url/title/snippet when possible). "
    "Keep content concise and evidence-backed. "
    "IMPORTANT: Respond in Chinese. Do NOT use Markdown formatting in the content field - use plain text only. "
    "Keep proper nouns and names in their original language."
)

# /grok 指令内置提示词：图片卡片模式（卡片渲染器按标题分面板，要求结构化 Markdown）
CMD_CARD_SYSTEM_PROMPT = (
    "You are a web research assistant. Use live web search/browsing when answering. "
    "Return ONLY a single JSON object with keys: "
    "content (string), sources (array of objects with url/title/snippet when possible). "
    "Format the content field as clean, well-structured Markdown: "
    "open with a one-line direct answer, then split the body into short sections using '## ' headings, "
    "use '- ' bullet lists for enumerations and '**bold**' for key terms. "
    "Keep paragraphs short; never output a single dense wall of text. "
    "IMPORTANT: Respond in Chinese. Keep proper nouns and names in their original language."
)
# 网页内容抓取提示词
FETCH_SYSTEM_PROMPT = (
    "You are a web content extraction expert. "
    "Fetch the given URL and convert the page content to well-structured Markdown. "
    "Rules: "
    "1) Preserve ALL original text content completely - do NOT summarize or omit anything. "
    "2) Maintain heading hierarchy (h1-h6 → #-######). "
    "3) Convert tables, lists, code blocks, links, and images to proper Markdown syntax. "
    "4) Remove ads, navigation, scripts, and non-content elements. "
    "5) Prepend a metadata header: source URL, page title, fetch time. "
    "6) Use UTF-8 encoding. Output ONLY the Markdown document, nothing else."
)

# 图片格式不支持时的标准错误返回
IMAGE_UNSUPPORTED_ERROR: dict[str, str] = {
    "error": "❌ 图片格式不支持。Grok 仅支持 JPEG、PNG、GIF、WebP 格式，请转换后再试。",
    "error_hint": "用户提供的图片格式无法识别或不受 xAI API 支持，"
    "请提示用户转换为 JPEG/PNG/GIF/WebP 格式后重试。",
}

# HTTP 状态码友好错误提示
HTTP_ERROR_HINTS: dict[int, str] = {
    400: "请求格式错误，请检查请求体或 extra_body 配置",
    401: "认证失败，请检查 api_key 是否正确",
    403: "访问被拒绝，API Key 无权限或已被封禁",
    404: "模型不存在或 API 端点错误，请检查 model 和 base_url",
    405: "请求方法不允许，请检查 API 端点配置",
    415: "请求体格式错误，请确保 Content-Type 为 application/json",
    422: "请求参数格式无效，请检查 extra_body 配置",
    429: "请求过于频繁，已触发速率限制，请稍后重试",
    500: "服务器内部错误",
    502: "网关错误，API 服务可能暂时不可用",
    503: "服务暂时不可用，请稍后重试",
}

# 默认可重试的 HTTP 状态码
DEFAULT_RETRYABLE_STATUS_CODES: set[int] = {429, 500, 502, 503, 504}

# 默认模型名（与 _conf_schema.json 保持一致）
DEFAULT_MODEL = "grok-4.1-fast"


# ─── 工具函数 ─────────────────────────────────────────────


def get_local_time_info() -> str:
    """获取本地时间信息，注入到搜索查询中提供时间上下文"""
    try:
        local_tz = datetime.now().astimezone().tzinfo
        local_now = datetime.now(local_tz)
    except Exception:
        local_now = datetime.now(timezone.utc)

    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays_cn[local_now.weekday()]

    return (
        f"[Current Time Context]\n"
        f"- Date: {local_now.strftime('%Y-%m-%d')} ({weekday})\n"
        f"- Time: {local_now.strftime('%H:%M:%S')}\n"
        f"- Timezone: {local_now.tzname() or 'Local'}\n"
    )


# ─── 搜索模式参数规范化 ────────────────────────────────

# 合法的搜索深度值
_VALID_SEARCH_DEPTHS = {"basic", "advanced", "deep"}
# 合法的时间范围值
_VALID_TIME_RANGES = {"day", "week", "month", "year"}
# 合法的主题值
_VALID_TOPICS = {"general", "news"}
# search_depth → 模式名映射
_DEPTH_MODE_MAP = {"basic": "quick", "advanced": "detailed", "deep": "deep"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_search_options(
    search_depth: str = "basic",
    max_results: int = 7,
    topic: str = "general",
    days: int = 0,
    time_range: str = "",
    start_date: str = "",
    end_date: str = "",
) -> dict[str, object]:
    """软校验搜索选项，非法值降级为安全默认值。

    Returns:
        规范化后的选项字典。
    """
    # search_depth — 仅允许已知值
    search_depth = str(search_depth).strip().lower()
    if search_depth not in _VALID_SEARCH_DEPTHS:
        search_depth = "basic"

    # max_results — clamp 5-20，非数字降级 7
    try:
        max_results = max(5, min(20, int(max_results)))
    except (ValueError, TypeError):
        max_results = 7

    # topic — 仅允许已知值
    topic = str(topic).strip().lower()
    if topic not in _VALID_TOPICS:
        topic = "general"

    # days — 0-365，非数字降级 0
    try:
        days = max(0, min(365, int(days)))
    except (ValueError, TypeError):
        days = 0

    # time_range — 仅允许已知值
    time_range = str(time_range).strip().lower()
    if time_range and time_range not in _VALID_TIME_RANGES:
        time_range = ""

    # 日期格式校验（YYYY-MM-DD）
    start_date = str(start_date).strip()
    end_date = str(end_date).strip()
    if start_date and not _DATE_PATTERN.match(start_date):
        start_date = ""
    if end_date and not _DATE_PATTERN.match(end_date):
        end_date = ""

    return {
        "search_depth": search_depth,
        "max_results": max_results,
        "topic": topic,
        "days": days,
        "time_range": time_range,
        "start_date": start_date,
        "end_date": end_date,
    }


def resolve_search_mode(search_depth: str) -> str:
    """将 search_depth 映射为模式名（quick / detailed / deep）。"""
    return _DEPTH_MODE_MAP.get(search_depth, "quick")


def resolve_mode_model(
    mode_model: str,
    fallback_model: str = "",
) -> str:
    """选择具体模型：模式专用模型 > 通用 model > DEFAULT_MODEL。"""
    return mode_model or fallback_model or DEFAULT_MODEL


def resolve_reasoning_params(search_depth: str) -> tuple[str | None, int | None]:
    """根据 search_depth 返回 (reasoning_effort, reasoning_budget_tokens)。

    basic   → 不开启思考（最快）
    advanced → 中等思考
    deep    → 深度思考 + 32k token 预算
    """
    if search_depth == "deep":
        return "high", 32000
    if search_depth == "advanced":
        return "medium", None
    return None, None


def build_search_time_constraints(
    topic: str = "general",
    days: int = 0,
    time_range: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """构建搜索时间约束提示词片段。

    优先级: start_date/end_date > time_range > days
    topic="news" 且无任何时间参数时默认 days=7。
    topic="general" 且无时间参数时仅返回当前时间上下文。
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    now = _dt.now().astimezone()
    today_str = now.strftime("%Y-%m-%d")

    computed_start = ""
    computed_end = ""

    if start_date or end_date:
        computed_start = start_date
        computed_end = end_date
    elif time_range:
        if time_range == "day":
            computed_start = today_str
        elif time_range == "week":
            computed_start = (now - _td(days=7)).strftime("%Y-%m-%d")
        elif time_range == "month":
            computed_start = (now - _td(days=30)).strftime("%Y-%m-%d")
        elif time_range == "year":
            computed_start = (now - _td(days=365)).strftime("%Y-%m-%d")
        computed_end = today_str
    elif days > 0:
        computed_start = (now - _td(days=days)).strftime("%Y-%m-%d")
        computed_end = today_str
    elif topic == "news":
        # 新闻主题默认最近 7 天
        computed_start = (now - _td(days=7)).strftime("%Y-%m-%d")
        computed_end = today_str

    lines: list[str] = []

    if computed_start or computed_end:
        lines.append("[Search Time Constraints]")
        lines.append(f"- Topic: {topic}")
        if computed_start and computed_end:
            lines.append(f"- Time window: {computed_start} to {computed_end}")
        elif computed_start:
            lines.append(f"- Start date: {computed_start}")
        elif computed_end:
            lines.append(f"- End date: {computed_end}")
        elif time_range:
            lines.append(f"- Time range: past {time_range}")
        lines.append(f"- Current date: {today_str}")
        lines.append("")

    return "\n".join(lines)


def parse_retry_after(headers: Any) -> float | None:
    """解析 Retry-After 响应头（支持秒数或 HTTP 日期格式）"""
    header = None
    if hasattr(headers, "get"):
        header = headers.get("Retry-After")
    if not header:
        return None
    header = str(header).strip()

    # 纯数字（秒数）
    if header.isdigit():
        return float(header)

    # HTTP 日期格式
    try:
        retry_dt = parsedate_to_datetime(header)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        delay = (retry_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delay)
    except (TypeError, ValueError):
        return None


def normalize_image(b64_data: str) -> tuple[str, str] | None:
    """检测图片格式，必要时转换为 API 支持的格式。

    xAI 支持: JPEG, PNG, GIF, WebP
    不支持的格式（BMP, TIFF 等）会尝试用 PIL 转为 PNG。
    无法识别的格式返回 None（调用方应报错拒绝）。

    Returns:
        (mime_type, base64_data) 或 None（格式无法识别）
    """
    import base64 as _b64

    _SUPPORTED = {"image/jpeg", "image/png", "image/gif", "image/webp"}

    # 先用 PIL 尝试（更准确，且能转换格式）
    try:
        from io import BytesIO

        from PIL import Image  # noqa: F811

        raw = _b64.b64decode(b64_data)
        img = Image.open(BytesIO(raw))

        fmt = (img.format or "").upper()
        _FMT_MAP = {
            "JPEG": "image/jpeg",
            "JPG": "image/jpeg",
            "PNG": "image/png",
            "GIF": "image/gif",
            "WEBP": "image/webp",
        }
        mime = _FMT_MAP.get(fmt, "")

        if mime in _SUPPORTED:
            return mime, b64_data

        # 不支持的格式 → 转 PNG
        buf = BytesIO()
        img = img.convert("RGBA") if img.mode in ("P", "LA", "PA") else img
        if img.mode == "RGBA":
            img.save(buf, format="PNG")
            new_b64 = _b64.b64encode(buf.getvalue()).decode()
            return "image/png", new_b64
        else:
            img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=90)
            new_b64 = _b64.b64encode(buf.getvalue()).decode()
            return "image/jpeg", new_b64
    except ImportError:
        pass  # PIL 不可用，回退到魔数字节检测
    except Exception:
        return None  # PIL 解码失败 → 图片损坏或格式不支持

    # 回退：魔数字节检测（不做转换）
    try:
        raw_header = _b64.b64decode(b64_data[:64], validate=False)
    except Exception:
        return None

    if raw_header[:3] == b"\xff\xd8\xff":
        return "image/jpeg", b64_data
    if raw_header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", b64_data
    if raw_header[:4] == b"GIF8":
        return "image/gif", b64_data
    if raw_header[:4] == b"RIFF" and raw_header[8:12] == b"WEBP":
        return "image/webp", b64_data
    return None  # 无法识别 → 拒绝


def build_user_content(
    text: str,
    images: list[str] | None,
    *,
    kind: str,
) -> Any:
    """构建用户消息体，自动处理多模态。

    - 无图片：返回纯文本字符串。
    - 有图片：返回内容数组；遇到无法识别的图片格式时返回 IMAGE_UNSUPPORTED_ERROR。

    kind:
        - "chat":      OpenAI Chat Completions 多模态格式（type=text/image_url）
        - "responses": xAI Responses API 多模态格式（type=input_text/input_image）
    """
    if not images:
        return text

    if kind == "chat":
        text_part: dict[str, Any] = {"type": "text", "text": text}
    elif kind == "responses":
        text_part = {"type": "input_text", "text": text}
    else:
        raise ValueError(f"Unknown content kind: {kind!r}")

    parts: list[dict[str, Any]] = [text_part]
    for img_b64 in images:
        normalized = normalize_image(img_b64)
        if normalized is None:
            return IMAGE_UNSUPPORTED_ERROR
        mime, img_b64 = normalized
        if kind == "chat":
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                }
            )
        else:
            parts.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{img_b64}",
                    "detail": "high",
                }
            )
    return parts


def safe_number(
    value: Any,
    default: float | int,
    *,
    cast: type = float,
    min_val: float | int | None = None,
) -> Any:
    """安全地把配置值转成数值；失败、None、低于下限时回退到 default。"""
    try:
        v = cast(value) if value is not None else default
    except (ValueError, TypeError):
        return default
    if min_val is not None and v < min_val:
        return default
    return v


def resolve_system_prompt(custom_prompt: Any, default_prompt: str) -> str:
    """选择系统提示词：自定义优先（去掉空白），否则使用默认。"""
    if isinstance(custom_prompt, str):
        stripped = custom_prompt.strip()
        if stripped:
            return stripped
    return default_prompt


def normalize_api_key(api_key: str) -> str:
    """过滤占位符 API Key"""
    api_key = api_key.strip()
    if not api_key:
        return ""
    placeholder = {"YOUR_API_KEY", "API_KEY", "CHANGE_ME", "REPLACE_ME"}
    if api_key.upper() in placeholder:
        return ""
    return api_key


def normalize_base_url(base_url: str) -> str:
    """规范化 Base URL：过滤占位符、去尾 / 和 /v1。空/占位符返回 ""。"""
    base_url = (base_url or "").strip()
    if not base_url:
        return ""
    placeholder = {
        "HTTPS://YOUR-GROK-ENDPOINT.EXAMPLE",
        "YOUR_BASE_URL",
        "BASE_URL",
        "CHANGE_ME",
        "REPLACE_ME",
    }
    if base_url.upper() in placeholder:
        return ""
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    return base_url


# 兼容别名（旧名义为"过滤占位符"，新版统一行为）
def normalize_base_url_value(base_url: str) -> str:
    """已合并到 normalize_base_url，保留以兼容外部调用。"""
    return normalize_base_url(base_url)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """尝试从字符串中解析出 JSON 对象（增强版）。

    支持：
    1. 纯 JSON 对象
    2. Markdown 代码块包裹的 JSON
    3. 混合文本中的 JSON（含嵌套结构，优先返回含 content/sources 的对象）
    """
    if not text or not text.strip():
        return None
    text = text.strip()

    # 1) 纯 JSON
    if text.startswith("{") and text.endswith("}"):
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    # 2) Markdown 代码块
    for match in re.findall(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text):
        try:
            value = json.loads(match.strip())
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue

    # 3) 混合文本，从每个 { 起点尝试解码，优先含 content/sources 的对象
    decoder = json.JSONDecoder()
    start_idx = 0
    max_attempts = 10
    while start_idx < len(text) and max_attempts > 0:
        brace_pos = text.find("{", start_idx)
        if brace_pos == -1:
            break
        try:
            value, end_idx = decoder.raw_decode(text, idx=brace_pos)
            if isinstance(value, dict) and ("content" in value or "sources" in value):
                return value
            start_idx = end_idx
        except json.JSONDecodeError:
            start_idx = brace_pos + 1
        max_attempts -= 1

    return None


def coerce_json_object(text: str) -> dict[str, Any] | None:
    """已合并到 parse_json_object，保留以兼容外部调用。"""
    return parse_json_object(text)


def normalize_sources(raw_sources: Any) -> list[dict[str, str]]:
    """归一化 sources 列表：仅保留 url 安全且为 dict 的条目。"""
    sources: list[dict[str, str]] = []
    if not isinstance(raw_sources, list):
        return sources
    for item in raw_sources:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        url = str(item.get("url", ""))
        if not is_safe_url(url):
            continue
        sources.append(
            {
                "url": url,
                "title": str(item.get("title") or ""),
                "snippet": str(item.get("snippet") or ""),
            }
        )
    return sources


def is_safe_url(url: str) -> bool:
    """校验 URL 是否安全：仅允许 http/https，长度<=2048，无控制字符"""
    from urllib.parse import urlparse

    if not url or len(url) > 2048:
        return False
    if any(ord(c) < 32 for c in url):
        return False
    try:
        return urlparse(url).scheme in ("http", "https")
    except Exception:
        return False


def extract_urls(text: str, *, safe_only: bool = True) -> list[str]:
    """从文本中提取 URL（默认仅返回通过 is_safe_url 校验的链接）"""
    urls = re.findall(r"https?://[^\s)\]}>\"']+", text)
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        url = url.rstrip(".,;:!?'\"")
        if not url or url in seen:
            continue
        if safe_only and not is_safe_url(url):
            continue
        seen.add(url)
        out.append(url)
    return out


def parse_json_config(value: str) -> tuple[dict[str, Any], str | None]:
    """解析 JSON 配置字符串

    Returns:
        (parsed_dict, error_message): 解析结果和错误信息，无错误时 error_message 为 None
    """
    if not value or not value.strip():
        return {}, None
    try:
        parsed = json.loads(value)
        return (parsed if isinstance(parsed, dict) else {}, None)
    except json.JSONDecodeError as e:
        return {}, f"JSON 配置解析失败: {e}"


# ─── 共享逻辑 ─────────────────────────────────────────────


def make_error_result(
    error: str,
    started: float,
    retries: int = 0,
    raw: str = "",
) -> dict[str, Any]:
    """构造标准化错误返回字典"""
    return {
        "ok": False,
        "error": error,
        "content": "",
        "sources": [],
        "raw": raw,
        "elapsed_ms": int((time.time() - started) * 1000),
        "retries": retries,
    }


def validate_config(
    base_url: str,
    api_key: str,
    started: float,
    *,
    base_url_label: str = "API 端点",
) -> dict[str, Any] | tuple[str, str]:
    """验证并规范化 base_url 和 api_key。

    Returns:
        错误 dict（验证失败）或 (normalized_base_url, normalized_api_key) 元组（成功）
    """
    base_url = normalize_base_url(base_url)
    api_key = normalize_api_key(api_key)

    if not base_url:
        return make_error_result(
            f"缺少 base_url 配置，请在插件设置中填写{base_url_label}",
            started,
        )
    if not api_key:
        return make_error_result(
            "缺少 api_key 配置，请在插件设置中填写 API 密钥",
            started,
        )
    return base_url, api_key


def build_headers(
    api_key: str,
    extra_headers: dict | None = None,
) -> dict[str, str]:
    """构建请求头，合并 extra_headers 并保护关键头"""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if extra_headers:
        protected = {"authorization", "content-type"}
        for key, value in extra_headers.items():
            if str(key).lower() not in protected:
                headers[str(key)] = str(value)
    return headers


def merge_extra_body(
    body: dict[str, Any],
    extra_body: dict | None,
    protected_keys: set[str],
) -> None:
    """将 extra_body 合并到 body 中，保护关键字段"""
    if extra_body:
        for key, value in extra_body.items():
            if key not in protected_keys:
                body[key] = value


def format_http_error(
    status: int,
    error_text: str,
    started: float,
    resp_headers: Any = None,
) -> dict[str, Any]:
    """格式化 HTTP 错误响应"""
    hint = HTTP_ERROR_HINTS.get(status, "")
    error_msg = f"HTTP {status}"
    if hint:
        error_msg = f"{error_msg} - {hint}"
    result = make_error_result(
        error_msg,
        started,
        raw=error_text[:2000] if error_text else "",
    )
    result["status"] = status
    # 429 时解析 Retry-After 头
    if status == 429 and resp_headers is not None:
        retry_after = parse_retry_after(resp_headers)
        if retry_after is not None:
            result["retry_after_seconds"] = retry_after
    return result


# ─── 正文装饰清理 ───────────────────────────────────────
# 部分中转/转发端点会把 Grok 实时搜索的终端风格装饰行泄漏进正文，
# 例如 "[ GROK DATA STREAM :: SEARCH ]"、"SYS.STATUS: ONLINE"，以及结尾的
# "MODEL :: ..."、"38.8s · 23185 tokens"。这些行在卡片与文本模式下都会渲染成噪音，
# 这里保守地清理正文首尾的装饰行（中间正文绝不动）。

# 首部和尾部的装饰行模式
_RE_STREAM_HEAD = re.compile(
    r"^\s*(?:\[\s*GROK[\s_]*DATA[\s_]*STREAM[^\]]*\]|SYS\.STATUS\s*:.*)$",
    re.IGNORECASE,
)
_RE_STREAM_TAIL = re.compile(
    r"^\s*(?:MODEL\s*::.*|\d+(?:\.\d+)?\s*s\s*[·•|]\s*\d[\d,]*\s*tokens?)$",
    re.IGNORECASE,
)
# 首尾各自最多清理的装饰行数，防止误伤正常正文
_STREAM_STRIP_MAX = 6


def strip_stream_decorations(text: str) -> str:
    """保守清理中转端点泄漏进正文的 Grok 实时搜索终端装饰行。

    仅处理正文最前端与最末端连续的装饰行（含相邻空行），中间内容永不改动。
    未匹配到任何装饰行、或清理后会导致正文为空时，原样返回。
    """
    if not text or not text.strip():
        return text
    lines = text.split("\n")
    n = len(lines)

    head = 0
    while head < n and head < _STREAM_STRIP_MAX:
        line = lines[head]
        if not line.strip() or _RE_STREAM_HEAD.match(line):
            head += 1
        else:
            break

    tail = n
    while tail > head and (n - tail) < _STREAM_STRIP_MAX:
        line = lines[tail - 1]
        if not line.strip() or _RE_STREAM_TAIL.match(line):
            tail -= 1
        else:
            break

    stripped = "\n".join(lines[head:tail])
    # 安全兜底：清理后正文为空则保留原文
    return stripped if stripped.strip() else text


def parse_sources_from_message(message: str) -> dict[str, Any]:
    """从 LLM 响应消息中解析 content 和 sources。

    Returns:
        {"content": str, "sources": list, "raw": str}
    """
    parsed = coerce_json_object(message)
    sources: list[dict[str, Any]] = []
    content = ""
    raw = ""

    if parsed is not None:
        content = strip_stream_decorations(str(parsed.get("content") or ""))
        src = parsed.get("sources")
        seen_urls: set[str] = set()
        if isinstance(src, list):
            for item in src:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                sources.append(
                    {
                        "url": url,
                        "title": str(item.get("title") or ""),
                        "snippet": str(item.get("snippet") or ""),
                    }
                )
        if not sources:
            for url_str in extract_urls(content):
                if url_str in seen_urls:
                    continue
                seen_urls.add(url_str)
                sources.append({"url": url_str, "title": "", "snippet": ""})
    else:
        raw = message
        content = strip_stream_decorations(message)
        for url_str in extract_urls(message):
            sources.append({"url": url_str, "title": "", "snippet": ""})

    return {"content": content, "sources": sources, "raw": raw}


async def retry_request(
    do_request: Any,
    *,
    proxy: str | None,
    max_retries: int,
    retry_delay: float,
    retryable_status_codes: set[int] | None,
    timeout: float,
    started: float,
) -> dict[str, Any]:
    """通用的带重试的请求执行器。

    Args:
        do_request: async callable(proxy) -> dict
        proxy: HTTP 代理
        max_retries: 最大重试次数
        retry_delay: 重试基础间隔
        retryable_status_codes: 可重试的 HTTP 状态码
        timeout: 超时秒数（用于错误消息）
        started: 起始 time.time()

    Returns:
        包含 ok/data/error 等字段的字典
    """
    if retryable_status_codes is None:
        retryable_status_codes = DEFAULT_RETRYABLE_STATUS_CODES

    result = None
    last_error = None
    retry_count = 0

    for attempt in range(max_retries + 1):
        try:
            result = await do_request(proxy)

            if result.get("ok"):
                break

            # 检查是否为可重试的错误：优先看 status 字段，其次回退到字符串包含
            status = result.get("status")
            if isinstance(status, int):
                should_retry = status in retryable_status_codes
            else:
                error_msg = result.get("error", "")
                should_retry = any(
                    f"HTTP {code}" in error_msg for code in retryable_status_codes
                )

            if should_retry and attempt < max_retries:
                retry_count = attempt + 1
                # 优先使用 Retry-After 头指定的等待时间
                wait_time = result.get("retry_after_seconds")
                if wait_time is None or not isinstance(wait_time, (int, float)):
                    wait_time = retry_delay * (attempt + 1)
                await asyncio.sleep(wait_time)
                continue

            break

        except aiohttp.ClientError as e:
            last_error = f"网络请求失败: {e}"
            if attempt < max_retries:
                retry_count = attempt + 1
                await asyncio.sleep(retry_delay * (attempt + 1))
                continue
            return make_error_result(last_error, started, retry_count)
        except TimeoutError:
            last_error = (
                f"请求超时（{timeout}秒），请检查网络或增加 timeout_seconds 配置"
            )
            if attempt < max_retries:
                retry_count = attempt + 1
                await asyncio.sleep(retry_delay * (attempt + 1))
                continue
            return make_error_result(last_error, started, retry_count)

    if result is None:
        return make_error_result(last_error or "未知错误", started, retry_count)

    if not result.get("ok") or "data" not in result:
        result["retries"] = retry_count
        return result

    result["retries"] = retry_count
    return result
