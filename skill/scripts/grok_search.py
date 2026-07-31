import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# ─── 从插件 tool.py 导入共享函数，避免重复维护 ───
_PLUGIN_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from tool import (  # noqa: E402
    DEFAULT_MODEL,
    FETCH_SYSTEM_PROMPT,
    build_search_time_constraints,
    get_local_time_info,
    normalize_search_options,
    resolve_mode_model,
    resolve_reasoning_params,
    resolve_search_mode,
    strip_stream_decorations,
)
from tool import (  # noqa: E402
    coerce_json_object as _coerce_json_object,
)
from tool import (  # noqa: E402
    extract_urls as _extract_urls,
)
from tool import (  # noqa: E402
    normalize_api_key as _normalize_api_key,
)
from tool import (  # noqa: E402
    normalize_base_url as _normalize_base_url,
)
from tool import (  # noqa: E402
    normalize_base_url_value as _normalize_base_url_value,
)
from tool import (  # noqa: E402
    normalize_image as _normalize_image,
)

# ─── 新版分组配置读取（与 main.py CONFIG_PATHS 保持一致）──────────────────
_CONFIG_PATHS = {
    "model": ("provider_settings", "model"),
    "use_responses_api": ("provider_settings", "use_responses_api"),
    "quick_model": ("provider_settings", "quick_model"),
    "detailed_model": ("provider_settings", "detailed_model"),
    "deep_model": ("provider_settings", "deep_model"),
    "base_url": ("connection_settings", "base_url"),
    "api_key": ("connection_settings", "api_key"),
    "timeout_seconds": ("connection_settings", "timeout_seconds"),
    "proxy": ("connection_settings", "proxy"),
    "max_retries": ("request_settings", "max_retries"),
    "retry_delay": ("request_settings", "retry_delay"),
    "retryable_status_codes": ("request_settings", "retryable_status_codes"),
    "custom_system_prompt": ("request_settings", "custom_system_prompt"),
    "enable_stream": ("request_settings", "enable_stream"),
    "extra_body": ("advanced_settings", "extra_body"),
    "extra_headers": ("advanced_settings", "extra_headers"),
    "show_sources": ("output_settings", "show_sources"),
    "render_as_image": ("output_settings", "render_as_image"),
    "markdown_plain_fallback": ("output_settings", "markdown_plain_fallback"),
    "send_as_forward": ("output_settings", "send_as_forward"),
    "card_theme": ("output_settings", "card_theme"),
    "max_sources": ("output_settings", "max_sources"),
    "enable_fetch": ("tool_settings", "enable_fetch"),
    "enable_skill": ("tool_settings", "enable_skill"),
}

_CONFIG_DEFAULTS = {
    "model": DEFAULT_MODEL,
    "use_responses_api": False,
    "quick_model": "",
    "detailed_model": "",
    "deep_model": "",
    "base_url": "",
    "api_key": "",
    "timeout_seconds": 60,
    "proxy": "",
    "max_retries": 3,
    "retry_delay": 1.0,
    "retryable_status_codes": [429, 500, 502, 503, 504],
    "custom_system_prompt": "",
    "enable_stream": False,
    "extra_body": "",
    "extra_headers": "",
    "show_sources": False,
    "render_as_image": False,
    "markdown_plain_fallback": True,
    "send_as_forward": False,
    "card_theme": "auto",
    "max_sources": 5,
    "enable_fetch": False,
    "enable_skill": False,
}


def _cfg(config: dict[str, Any], key: str):
    """优先从分组配置读取，fallback 到平铺键和默认值"""
    path = _CONFIG_PATHS.get(key)
    if path:
        section = config.get(path[0])
        if isinstance(section, dict) and path[1] in section:
            return section[path[1]]
    default = _CONFIG_DEFAULTS.get(key)
    return config.get(key, default)


def _compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _default_user_config_path() -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, ".codex", "config", "grok-search.json")


def _skill_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _find_astrbot_data_path() -> str:
    """尝试查找 AstrBot data 目录路径"""
    # 方式1: 从 skill 目录向上查找 (data/skills/grok-search/scripts -> data)
    current = os.path.dirname(__file__)
    for _ in range(5):
        parent = os.path.dirname(current)
        if os.path.basename(parent) == "data" and os.path.isdir(
            os.path.join(parent, "config")
        ):
            return parent
        # 检查是否在 skills 目录下
        if os.path.basename(current) == "skills" and os.path.isdir(
            os.path.join(os.path.dirname(current), "config")
        ):
            return os.path.dirname(current)
        current = parent

    # 方式2: 环境变量
    astrbot_data = os.environ.get("ASTRBOT_DATA_PATH", "").strip()
    if astrbot_data and os.path.isdir(astrbot_data):
        return astrbot_data

    return ""


def _load_astrbot_plugin_config() -> tuple[dict[str, Any], str]:
    """加载 AstrBot 插件配置

    Returns:
        (config_dict, status_message)
        status_message: 空字符串表示成功，否则为错误/警告信息
    """
    data_path = _find_astrbot_data_path()
    if not data_path:
        return {}, "AstrBot data 目录未找到"

    config_path = os.path.join(
        data_path, "config", "astrbot_plugin_grok_web_search.json"
    )
    if not os.path.exists(config_path):
        return {}, f"AstrBot 插件配置文件不存在: {config_path}"

    try:
        with open(config_path, encoding="utf-8-sig") as f:
            raw_config = json.load(f)
        # AstrBot 配置格式: {"key": {"value": actual_value, ...}}
        if isinstance(raw_config, dict):
            result = {}
            for key, item in raw_config.items():
                if isinstance(item, dict) and "value" in item:
                    result[key] = item["value"]
                else:
                    result[key] = item
            return result, ""
    except json.JSONDecodeError as e:
        return {}, f"AstrBot 插件配置 JSON 解析失败: {e}"
    except Exception as e:
        return {}, f"AstrBot 插件配置读取失败: {e}"
    return {}, "AstrBot 插件配置格式异常"


def _default_skill_config_paths() -> list[str]:
    root = _skill_root()
    return [
        os.path.join(root, "config.json"),
        os.path.join(root, "config.local.json"),
    ]


def _load_json_file(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            value = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("config must be a JSON object")
    return value


def _load_json_env(var_name: str) -> dict[str, Any]:
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{var_name} must be a JSON object")
    return value


def _parse_json_object(raw: str, *, label: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _parse_sse_response(raw_text: str) -> dict[str, Any] | None:
    """解析 SSE 流式响应，合并所有 chunk 的内容"""
    chunks: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                continue
            try:
                chunk = json.loads(data_str)
                if isinstance(chunk, dict):
                    chunks.append(chunk)
            except json.JSONDecodeError:
                continue

    if not chunks:
        return None

    # 合并所有 chunk 的 delta content
    merged_content = ""
    model_name = ""
    usage_info = {}

    for chunk in chunks:
        if not model_name:
            model_name = chunk.get("model", "")
        if chunk.get("usage"):
            usage_info = chunk["usage"]

        choices = chunk.get("choices", [])
        if choices and isinstance(choices, list):
            choice = choices[0]
            delta = choice.get("delta", {})
            if delta and isinstance(delta, dict):
                content = delta.get("content", "")
                if content:
                    merged_content += content

    return {
        "choices": [{"message": {"content": merged_content}}],
        "model": model_name,
        "usage": usage_info,
    }


def _request_chat_completions(
    *,
    base_url: str,
    api_key: str,
    model: str,
    query: str,
    timeout_seconds: float,
    reasoning_effort: str | None,
    reasoning_budget_tokens: int | None,
    extra_headers: dict[str, Any],
    extra_body: dict[str, Any],
    stream: bool = False,
    images: list[str] | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    url = f"{_normalize_base_url(base_url)}/v1/chat/completions"

    system = system_prompt or (
        "You are a web research assistant. Use live web search/browsing when answering. "
        "Return ONLY a single JSON object with keys: "
        "content (string), sources (array of objects with url/title/snippet when possible). "
        "Keep content concise and evidence-backed. "
        "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
    )

    # 注入时间上下文
    time_context = get_local_time_info()
    enriched_query = f"{time_context}\n{query}"

    # Build user message: multimodal format when images are present
    if images:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": enriched_query}]
        for img_b64 in images:
            result = _normalize_image(img_b64)
            if result is None:
                return {
                    "error": "❌ 图片格式不支持。Grok 仅支持 JPEG、PNG、GIF、WebP 格式，"
                    "请转换后再试。",
                    "error_hint": "用户提供的图片格式无法识别或不受 xAI API 支持，"
                    "请提示用户转换为 JPEG/PNG/GIF/WebP 格式后重试。",
                }
            mime, img_b64 = result
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                }
            )
        user_message: dict[str, Any] = {"role": "user", "content": user_content}
    else:
        user_message = {"role": "user", "content": enriched_query}

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            user_message,
        ],
        "temperature": 0.2,
        "stream": stream,
    }

    # 添加思考模式参数
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
        if reasoning_budget_tokens:
            body["reasoning_budget_tokens"] = reasoning_budget_tokens

    # 合并 extra_body，保护核心字段不被覆盖
    _protected = {
        "model",
        "messages",
        "stream",
        "reasoning_effort",
        "reasoning_budget_tokens",
    }
    for _key, _value in extra_body.items():
        if _key not in _protected:
            body[_key] = _value

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    for key, value in extra_headers.items():
        headers[str(key)] = str(value)

    req = urllib.request.Request(
        url=url,
        data=_compact_json(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw_text = resp.read().decode("utf-8", errors="replace")
        content_type = resp.headers.get("Content-Type", "")

        # 检查是否为 SSE 流式响应
        is_sse = "text/event-stream" in content_type or raw_text.strip().startswith(
            "data:"
        )

        if is_sse:
            parsed = _parse_sse_response(raw_text)
            if parsed:
                return parsed
            raise ValueError("SSE 流式响应解析失败")

        return json.loads(raw_text)


def _request_responses_api(
    *,
    base_url: str,
    api_key: str,
    model: str,
    query: str,
    timeout_seconds: float,
    extra_headers: dict[str, Any],
    extra_body: dict[str, Any],
    images: list[str] | None = None,
) -> dict[str, Any]:
    """通过 xAI Responses API (/v1/responses) 发起搜索请求"""
    url = f"{_normalize_base_url(base_url)}/v1/responses"

    system = (
        "You are a web research assistant. Use live web search/browsing when answering. "
        "Return ONLY a single JSON object with keys: "
        "content (string), sources (array of objects with url/title/snippet when possible). "
        "Keep content concise and evidence-backed. "
        "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
    )

    # 注入时间上下文
    time_context = get_local_time_info()
    enriched_query = f"{time_context}\n{query}"

    # Build user input for Responses API
    if images:
        user_content: list[dict[str, Any]] = [
            {"type": "input_text", "text": enriched_query}
        ]
        for img_b64 in images:
            result = _normalize_image(img_b64)
            if result is None:
                return {
                    "error": "❌ 图片格式不支持。Grok 仅支持 JPEG、PNG、GIF、WebP 格式，"
                    "请转换后再试。",
                    "error_hint": "用户提供的图片格式无法识别或不受 xAI API 支持，"
                    "请提示用户转换为 JPEG/PNG/GIF/WebP 格式后重试。",
                }
            mime, img_b64 = result
            user_content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{img_b64}",
                    "detail": "high",
                }
            )
        user_input: str | list[dict[str, Any]] = user_content
    else:
        user_input = enriched_query

    body: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ],
        "tools": [
            {"type": "web_search"},
            {"type": "x_search"},
        ],
    }

    # extra_body 合并（保护核心字段）
    protected_keys = {"model", "input", "tools", "stream"}
    for key, value in extra_body.items():
        if key not in protected_keys:
            body[key] = value

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    for key, value in extra_headers.items():
        headers[str(key)] = str(value)

    req = urllib.request.Request(
        url=url,
        data=_compact_json(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw_text = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw_text)


def _parse_responses_api_result(
    resp: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """解析 Responses API 响应，提取 message 文本和 citations

    Returns:
        (message_text, citations_list)
    """
    message = ""
    citations: list[dict[str, Any]] = []

    output = resp.get("output", [])
    for item in output:
        if item.get("type") == "message":
            for content_item in item.get("content", []):
                if content_item.get("type") == "output_text":
                    message = content_item.get("text", "")
                    for ann in content_item.get("annotations", []):
                        if ann.get("type") == "url_citation":
                            citations.append(
                                {
                                    "url": ann.get("url", ""),
                                    "title": ann.get("title", ""),
                                }
                            )
                    break
            break

    # 提取顶层 citations（纯 URL 列表）
    top_citations = resp.get("citations", [])
    if isinstance(top_citations, list):
        for url_str in top_citations:
            if isinstance(url_str, str) and url_str.startswith("http"):
                if not any(c.get("url") == url_str for c in citations):
                    citations.append({"url": url_str, "title": ""})

    return message, citations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggressive web research via OpenAI-compatible Grok endpoint."
    )
    parser.add_argument("--query", default="", help="Search query / research task.")
    parser.add_argument("--config", default="", help="Path to config JSON file.")
    parser.add_argument("--base-url", default="", help="Override base URL.")
    parser.add_argument("--api-key", default="", help="Override API key.")
    parser.add_argument("--model", default="", help="Override model.")
    parser.add_argument(
        "--timeout-seconds", type=float, default=0.0, help="Override timeout (seconds)."
    )
    parser.add_argument(
        "--search-depth",
        type=str,
        default="",
        help="Search depth: basic, advanced, or deep.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=0,
        help="Desired number of results (5-20).",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default="",
        help="Search topic: general or news.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Days to look back from today.",
    )
    parser.add_argument(
        "--time-range",
        type=str,
        default="",
        help="Time range: day, week, month, or year.",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default="",
        help="Start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default="",
        help="End date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--extra-body-json",
        default="",
        help="Extra JSON object merged into request body.",
    )
    parser.add_argument(
        "--extra-headers-json",
        default="",
        help="Extra JSON object merged into request headers.",
    )
    parser.add_argument(
        "--image-files",
        default="",
        help="Comma-separated image file paths for multimodal queries.",
    )
    parser.add_argument(
        "--fetch-url",
        default="",
        help="URL to fetch and convert to Markdown (fetch mode, replaces --query).",
    )
    args = parser.parse_args()

    env_config_path = os.environ.get("GROK_CONFIG_PATH", "").strip()
    explicit_config_path = args.config.strip() or env_config_path

    config_path = ""
    config: dict[str, Any] = {}
    astrbot_config_status = ""

    # 优先尝试加载 AstrBot 插件配置
    astrbot_config, astrbot_config_status = _load_astrbot_plugin_config()
    if astrbot_config and _normalize_api_key(
        str(_cfg(astrbot_config, "api_key") or "")
    ):
        config_path = "[AstrBot Plugin Config]"
        config = astrbot_config

    elif explicit_config_path:
        config_path = explicit_config_path
        try:
            config = _load_json_file(config_path)
        except Exception as e:
            sys.stderr.write(f"Invalid config ({config_path}): {e}\n")
            return 2
    else:
        fallback_path = ""
        fallback_config: dict[str, Any] = {}
        for candidate in [*_default_skill_config_paths(), _default_user_config_path()]:
            if not os.path.exists(candidate):
                continue
            try:
                candidate_config = _load_json_file(candidate)
            except Exception as e:
                sys.stderr.write(f"Invalid config ({candidate}): {e}\n")
                return 2

            if not fallback_path:
                fallback_path = candidate
                fallback_config = candidate_config

            candidate_key = _normalize_api_key(
                str(_cfg(candidate_config, "api_key") or "")
            )
            if candidate_key:
                config_path = candidate
                config = candidate_config
                break

        if not config_path and fallback_path:
            config_path = fallback_path
            config = fallback_config

        if not config_path:
            config_path = _default_skill_config_paths()[0]

    base_url = _normalize_base_url_value(
        args.base_url.strip()
        or os.environ.get("GROK_BASE_URL", "").strip()
        or str(_cfg(config, "base_url") or "").strip()
    )
    api_key = _normalize_api_key(
        args.api_key.strip()
        or os.environ.get("GROK_API_KEY", "").strip()
        or str(_cfg(config, "api_key") or "").strip()
    )
    model = (
        args.model.strip()
        or os.environ.get("GROK_MODEL", "").strip()
        or str(_cfg(config, "model") or "").strip()
        or DEFAULT_MODEL
    )

    # 使用共享规范化函数统一校验所有搜索选项
    opts = normalize_search_options(
        search_depth=args.search_depth.strip() or "basic",
        max_results=args.max_results or 7,
        topic=args.topic.strip() or "general",
        days=args.days,
        time_range=args.time_range.strip(),
        start_date=args.start_date.strip(),
        end_date=args.end_date.strip(),
    )
    search_depth = str(opts["search_depth"])
    max_results = int(str(opts["max_results"]))
    topic = str(opts["topic"])
    days = int(str(opts["days"]))
    time_range = str(opts["time_range"])
    start_date = str(opts["start_date"])
    end_date = str(opts["end_date"])

    # 解析模式及对应模型
    mode = resolve_search_mode(search_depth)
    mode_model = resolve_mode_model(
        str(_cfg(config, f"{mode}_model") or ""),
        model,
    )

    # 推理参数
    reasoning_effort, reasoning_budget_tokens = resolve_reasoning_params(search_depth)

    timeout_seconds = args.timeout_seconds
    if not timeout_seconds:
        try:
            timeout_seconds = float(os.environ.get("GROK_TIMEOUT_SECONDS", "0") or "0")
        except (ValueError, TypeError):
            timeout_seconds = 0.0
    if not timeout_seconds:
        try:
            timeout_seconds = float(_cfg(config, "timeout_seconds") or 0)
        except (ValueError, TypeError):
            timeout_seconds = 0.0
    if not timeout_seconds or timeout_seconds <= 0:
        timeout_seconds = 60.0

    # 解析 Responses API 开关
    use_responses_api = False
    cfg_use_responses = _cfg(config, "use_responses_api")
    if isinstance(cfg_use_responses, bool):
        use_responses_api = cfg_use_responses

    if not base_url:
        sys.stderr.write(
            "Missing base URL: set GROK_BASE_URL, write it to config, or pass --base-url\n"
            f"Config path: {config_path}\n"
        )
        if astrbot_config_status:
            sys.stderr.write(f"AstrBot config status: {astrbot_config_status}\n")
        return 2

    if not api_key:
        sys.stderr.write(
            "Missing API key: set GROK_API_KEY, write it to config, or pass --api-key\n"
            f"Config path: {config_path}\n"
        )
        if astrbot_config_status:
            sys.stderr.write(f"AstrBot config status: {astrbot_config_status}\n")
        return 2

    try:
        extra_body: dict[str, Any] = {}
        cfg_extra_body = _cfg(config, "extra_body")
        if isinstance(cfg_extra_body, dict):
            extra_body.update(cfg_extra_body)
        extra_body.update(_load_json_env("GROK_EXTRA_BODY_JSON"))
        extra_body.update(
            _parse_json_object(args.extra_body_json, label="--extra-body-json")
        )

        extra_headers: dict[str, Any] = {}
        cfg_extra_headers = _cfg(config, "extra_headers")
        if isinstance(cfg_extra_headers, dict):
            extra_headers.update(cfg_extra_headers)
        extra_headers.update(_load_json_env("GROK_EXTRA_HEADERS_JSON"))
        extra_headers.update(
            _parse_json_object(args.extra_headers_json, label="--extra-headers-json")
        )
    except Exception as e:
        sys.stderr.write(f"Invalid JSON: {e}\n")
        return 2

    # Read image files and convert to base64
    images: list[str] = []
    if args.image_files:
        for img_path in args.image_files.split(","):
            img_path = img_path.strip()
            if not img_path:
                continue
            if not os.path.exists(img_path):
                sys.stderr.write(f"Image file not found: {img_path}\n")
                continue
            try:
                with open(img_path, "rb") as f:
                    img_data = base64.b64encode(f.read()).decode("utf-8")
                images.append(img_data)
            except Exception as e:
                sys.stderr.write(f"Failed to read image file {img_path}: {e}\n")

    started = time.time()

    # 判断运行模式：fetch 模式 vs search 模式
    fetch_url = args.fetch_url.strip() if hasattr(args, "fetch_url") else ""
    is_fetch_mode = bool(fetch_url)

    if is_fetch_mode:
        # Fetch 模式：抓取网页内容
        if not fetch_url.startswith("http"):
            sys.stderr.write("Error: --fetch-url must be a full HTTP/HTTPS URL\n")
            return 2
        query = f"{fetch_url}\n获取该网页内容并返回其结构化 Markdown 格式"
    else:
        # Search 模式：需要 --query
        if not args.query:
            sys.stderr.write(
                "Error: --query is required (or use --fetch-url for fetch mode)\n"
            )
            return 2
        query = args.query

        # 构建时间约束提示词并注入搜索引导
        time_constraints = build_search_time_constraints(
            topic=topic,
            days=days,
            time_range=time_range,
            start_date=start_date,
            end_date=end_date,
        )
        if time_constraints:
            query = f"{time_constraints}\n{query}"

        depth_guide = {
            "basic": "Provide a quick, concise overview.",
            "advanced": "Conduct thorough research with multiple sources.",
            "deep": "Perform an exhaustive, in-depth analysis with maximum sources.",
        }.get(search_depth, "")
        if depth_guide:
            query = (
                f"[Search Guide]\n"
                f"- Depth: {search_depth} ({depth_guide})\n"
                f"- Desired results: {max_results}\n"
                f"\n{query}"
            )

    try:
        if use_responses_api and not is_fetch_mode:
            resp = _request_responses_api(
                base_url=base_url,
                api_key=api_key,
                model=mode_model,
                query=query,
                timeout_seconds=timeout_seconds,
                extra_headers=extra_headers,
                extra_body=extra_body,
                images=images or None,
            )
        else:
            # Chat Completions 模式（search 和 fetch 都用这个）
            resp = _request_chat_completions(
                base_url=base_url,
                api_key=api_key,
                model=mode_model if not is_fetch_mode else model,
                query=query,
                timeout_seconds=timeout_seconds,
                reasoning_effort=reasoning_effort if not is_fetch_mode else None,
                reasoning_budget_tokens=reasoning_budget_tokens
                if not is_fetch_mode
                else None,
                stream=bool(_cfg(config, "enable_stream"))
                if not is_fetch_mode
                else False,
                extra_headers=extra_headers,
                extra_body=extra_body,
                images=images or None,
                system_prompt=FETCH_SYSTEM_PROMPT if is_fetch_mode else None,
            )
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        out = {
            "ok": False,
            "error": f"HTTP {getattr(e, 'code', None)}",
            "detail": raw or str(e),
            "config_path": config_path,
            "config_status": astrbot_config_status if astrbot_config_status else "OK",
            "model": model,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        sys.stdout.write(_compact_json(out))
        return 1
    except Exception as e:
        out = {
            "ok": False,
            "error": "request_failed",
            "detail": str(e),
            "config_path": config_path,
            "config_status": astrbot_config_status if astrbot_config_status else "OK",
            "model": model,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        sys.stdout.write(_compact_json(out))
        return 1

    # 检查 API 错误响应
    if "error" in resp and isinstance(resp.get("error"), (dict, str)):
        error_info = resp["error"]
        error_msg = (
            error_info.get("message", str(error_info))
            if isinstance(error_info, dict)
            else str(error_info)
        )
        out = {
            "ok": False,
            "error": "api_error",
            "detail": error_msg,
            "config_path": config_path,
            "config_status": astrbot_config_status if astrbot_config_status else "OK",
            "model": model,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        sys.stdout.write(_compact_json(out))
        return 1

    # 根据 API 模式解析响应
    message = ""
    api_citations: list[dict[str, Any]] = []

    if use_responses_api:
        message, api_citations = _parse_responses_api_result(resp)
    else:
        try:
            choice0 = (resp.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            message = msg.get("content") or ""
        except Exception:
            message = ""

    # 空响应检查
    if not message:
        out = {
            "ok": False,
            "error": "empty_response",
            "detail": "API 返回空内容",
            "config_path": config_path,
            "config_status": astrbot_config_status if astrbot_config_status else "OK",
            "model": model,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        sys.stdout.write(_compact_json(out))
        return 1

    # Fetch 模式：直接返回原始 Markdown 内容，不做 JSON 解析
    if is_fetch_mode:
        out = {
            "ok": True,
            "fetch_url": fetch_url,
            "config_path": config_path,
            "model": resp.get("model") or model,
            "content": message,
            "usage": resp.get("usage") or {},
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        sys.stdout.write(_compact_json(out))
        return 0

    parsed = _coerce_json_object(message)
    sources: list[dict[str, Any]] = []
    content = ""
    raw = ""
    # 去重集合提到分支外，JSON 与非 JSON 两条路径统一按 url 保序去重
    seen_urls: set[str] = set()

    if parsed is not None:
        content = strip_stream_decorations(str(parsed.get("content") or ""))
        src = parsed.get("sources")
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
            for url in _extract_urls(content):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sources.append({"url": url, "title": "", "snippet": ""})
    else:
        # 非 JSON 响应：将原始消息作为 content
        raw = message
        content = strip_stream_decorations(message)
        for url in _extract_urls(message):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append({"url": url, "title": "", "snippet": ""})

    # 补充 Responses API 的 citations 到 sources
    if not sources and api_citations:
        for cit in api_citations:
            url = str(cit.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(
                {
                    "url": url,
                    "title": str(cit.get("title") or ""),
                    "snippet": "",
                }
            )

    out = {
        "ok": True,
        "query": args.query or fetch_url,
        "config_path": config_path,
        "model": resp.get("model") or model,
        "content": content,
        "sources": sources,
        "raw": raw,
        "usage": resp.get("usage") or {},
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    sys.stdout.write(_compact_json(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
