import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any


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


def _normalize_api_key(api_key: str) -> str:
    api_key = api_key.strip()
    if not api_key:
        return ""
    placeholder = {"YOUR_API_KEY", "API_KEY", "CHANGE_ME", "REPLACE_ME"}
    if api_key.upper() in placeholder:
        return ""
    return api_key


def _normalize_base_url_value(base_url: str) -> str:
    base_url = base_url.strip()
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
    return base_url


def _load_json_file(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            value = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("config must be a JSON object")
    return value


def _normalize_base_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    if base_url.endswith("/v1"):
        return base_url[: -len("/v1")]
    return base_url


def _coerce_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s)\]}>\"']+", text)
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        url = url.rstrip(".,;:!?'\"")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


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
    enable_thinking: bool,
    thinking_budget: int,
    extra_headers: dict[str, Any],
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    url = f"{_normalize_base_url(base_url)}/v1/chat/completions"

    system = (
        "You are a web research assistant. Use live web search/browsing when answering. "
        "Return ONLY a single JSON object with keys: "
        "content (string), sources (array of objects with url/title/snippet when possible). "
        "Keep content concise and evidence-backed. "
        "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
    )

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        "temperature": 0.2,
        "stream": False,
    }

    # 添加思考模式参数
    if enable_thinking:
        body["reasoning_effort"] = "high"
        if thinking_budget > 0:
            body["reasoning_budget_tokens"] = thinking_budget

    body.update(extra_body)

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
        is_sse = (
            "text/event-stream" in content_type
            or raw_text.strip().startswith("data:")
        )

        if is_sse:
            parsed = _parse_sse_response(raw_text)
            if parsed:
                return parsed
            raise ValueError("SSE 流式响应解析失败")

        return json.loads(raw_text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggressive web research via OpenAI-compatible Grok endpoint."
    )
    parser.add_argument("--query", required=True, help="Search query / research task.")
    parser.add_argument("--config", default="", help="Path to config JSON file.")
    parser.add_argument("--base-url", default="", help="Override base URL.")
    parser.add_argument("--api-key", default="", help="Override API key.")
    parser.add_argument("--model", default="", help="Override model.")
    parser.add_argument(
        "--timeout-seconds", type=float, default=0.0, help="Override timeout (seconds)."
    )
    parser.add_argument(
        "--enable-thinking",
        type=str,
        default="",
        help="Enable thinking mode (true/false).",
    )
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=0,
        help="Thinking token budget.",
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
    args = parser.parse_args()

    env_config_path = os.environ.get("GROK_CONFIG_PATH", "").strip()
    explicit_config_path = args.config.strip() or env_config_path

    config_path = ""
    config: dict[str, Any] = {}
    astrbot_config_status = ""

    # 优先尝试加载 AstrBot 插件配置
    astrbot_config, astrbot_config_status = _load_astrbot_plugin_config()
    if astrbot_config and _normalize_api_key(str(astrbot_config.get("api_key") or "")):
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
                str(candidate_config.get("api_key") or "")
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
        or str(config.get("base_url") or "").strip()
    )
    api_key = _normalize_api_key(
        args.api_key.strip()
        or os.environ.get("GROK_API_KEY", "").strip()
        or str(config.get("api_key") or "").strip()
    )
    model = (
        args.model.strip()
        or os.environ.get("GROK_MODEL", "").strip()
        or str(config.get("model") or "").strip()
        or "grok-4-fast"
    )

    timeout_seconds = args.timeout_seconds
    if not timeout_seconds:
        try:
            timeout_seconds = float(os.environ.get("GROK_TIMEOUT_SECONDS", "0") or "0")
        except (ValueError, TypeError):
            timeout_seconds = 0.0
    if not timeout_seconds:
        try:
            timeout_seconds = float(config.get("timeout_seconds") or 0)
        except (ValueError, TypeError):
            timeout_seconds = 0.0
    if not timeout_seconds or timeout_seconds <= 0:
        timeout_seconds = 60.0

    # 解析思考模式配置
    enable_thinking_str = (
        args.enable_thinking.strip().lower()
        or os.environ.get("GROK_ENABLE_THINKING", "").strip().lower()
    )
    if enable_thinking_str in ("true", "1", "yes"):
        enable_thinking = True
    elif enable_thinking_str in ("false", "0", "no"):
        enable_thinking = False
    else:
        # 从配置文件读取，默认 True
        cfg_enable_thinking = config.get("enable_thinking")
        enable_thinking = cfg_enable_thinking if isinstance(cfg_enable_thinking, bool) else True

    thinking_budget = args.thinking_budget
    if not thinking_budget:
        try:
            thinking_budget = int(os.environ.get("GROK_THINKING_BUDGET", "0") or "0")
        except (ValueError, TypeError):
            thinking_budget = 0
    if not thinking_budget:
        try:
            thinking_budget = int(config.get("thinking_budget") or 0)
        except (ValueError, TypeError):
            thinking_budget = 0
    if not thinking_budget or thinking_budget <= 0:
        thinking_budget = 32000

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
        cfg_extra_body = config.get("extra_body")
        if isinstance(cfg_extra_body, dict):
            extra_body.update(cfg_extra_body)
        extra_body.update(_load_json_env("GROK_EXTRA_BODY_JSON"))
        extra_body.update(
            _parse_json_object(args.extra_body_json, label="--extra-body-json")
        )

        extra_headers: dict[str, Any] = {}
        cfg_extra_headers = config.get("extra_headers")
        if isinstance(cfg_extra_headers, dict):
            extra_headers.update(cfg_extra_headers)
        extra_headers.update(_load_json_env("GROK_EXTRA_HEADERS_JSON"))
        extra_headers.update(
            _parse_json_object(args.extra_headers_json, label="--extra-headers-json")
        )
    except Exception as e:
        sys.stderr.write(f"Invalid JSON: {e}\n")
        return 2

    started = time.time()
    try:
        resp = _request_chat_completions(
            base_url=base_url,
            api_key=api_key,
            model=model,
            query=args.query,
            timeout_seconds=timeout_seconds,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            extra_headers=extra_headers,
            extra_body=extra_body,
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

    message = ""
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

    parsed = _coerce_json_object(message)
    sources: list[dict[str, Any]] = []
    content = ""
    raw = ""

    if parsed is not None:
        content = str(parsed.get("content") or "")
        src = parsed.get("sources")
        if isinstance(src, list):
            for item in src:
                if isinstance(item, dict) and item.get("url"):
                    sources.append(
                        {
                            "url": str(item.get("url")),
                            "title": str(item.get("title") or ""),
                            "snippet": str(item.get("snippet") or ""),
                        }
                    )
        if not sources:
            for url in _extract_urls(content):
                sources.append({"url": url, "title": "", "snippet": ""})
    else:
        # 非 JSON 响应：将原始消息作为 content
        raw = message
        content = message
        for url in _extract_urls(message):
            sources.append({"url": url, "title": "", "snippet": ""})

    out = {
        "ok": True,
        "query": args.query,
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
