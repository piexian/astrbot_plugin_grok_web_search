"""
Grok API 异步客户端

通过 OpenAI 兼容接口调用 Grok 进行联网搜索
"""

import json
import re
import time
from typing import Any

import aiohttp


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
    """规范化 Base URL，移除尾部 / 和 /v1"""
    base_url = base_url.strip().rstrip("/")
    if base_url.endswith("/v1"):
        return base_url[: -len("/v1")]
    return base_url


def _normalize_base_url_value(base_url: str) -> str:
    """过滤占位符 Base URL"""
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


def _coerce_json_object(text: str) -> dict[str, Any] | None:
    """尝试将字符串解析为 JSON 对象"""
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
    """从文本中提取 URL"""
    urls = re.findall(r"https?://[^\s)\]}>\"']+", text)
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        url = url.rstrip(".,;:!?'\"")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def parse_json_config(value: str) -> dict[str, Any]:
    """解析 JSON 配置字符串，解析失败返回空字典并记录警告"""
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError as e:
        # 静默返回空字典，但可通过日志排查
        import sys

        print(f"[grok_client] JSON 配置解析失败: {e}", file=sys.stderr)
        return {}


async def grok_search(
    query: str,
    base_url: str,
    api_key: str,
    model: str = "grok-4-expert",
    timeout: float = 60.0,
    extra_body: dict | None = None,
    extra_headers: dict | None = None,
) -> dict[str, Any]:
    """
    调用 Grok API 进行联网搜索（异步）

    Args:
        query: 搜索查询内容
        base_url: Grok API 端点
        api_key: API 密钥
        model: 模型名称
        timeout: 超时时间（秒）
        extra_body: 额外请求体参数
        extra_headers: 额外请求头

    Returns:
        {
            "ok": bool,
            "content": str,      # 综合答案
            "sources": list,     # 来源列表 [{url, title, snippet}]
            "raw": str,          # 原始响应（解析失败时）
            "error": str,        # 错误信息（失败时）
            "elapsed_ms": int,   # 耗时
        }
    """
    started = time.time()

    # 验证必要参数
    base_url = _normalize_base_url_value(base_url)
    api_key = normalize_api_key(api_key)

    if not base_url:
        return {
            "ok": False,
            "error": "missing_base_url",
            "content": "",
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    if not api_key:
        return {
            "ok": False,
            "error": "missing_api_key",
            "content": "",
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    url = f"{normalize_base_url(base_url)}/v1/chat/completions"

    system_prompt = (
        "You are a web research assistant. Use live web search/browsing when answering. "
        "Return ONLY a single JSON object with keys: "
        "content (string), sources (array of objects with url/title/snippet when possible). "
        "Keep content concise and evidence-backed. "
        "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
    )

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    if extra_body:
        body.update(extra_body)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if extra_headers:
        for key, value in extra_headers.items():
            headers[str(key)] = str(value)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return {
                        "ok": False,
                        "error": f"HTTP {resp.status}",
                        "content": "",
                        "sources": [],
                        "raw": error_text[:2000] if error_text else "",
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

                # 尝试解析 JSON 响应
                try:
                    data = await resp.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                    raw_text = await resp.text()
                    return {
                        "ok": False,
                        "error": "invalid_json_response",
                        "content": str(e),
                        "sources": [],
                        "raw": raw_text[:2000] if raw_text else "",
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

    except aiohttp.ClientError as e:
        return {
            "ok": False,
            "error": "request_failed",
            "content": str(e),
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    except TimeoutError:
        return {
            "ok": False,
            "error": "timeout",
            "content": "",
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    # 解析响应
    message = ""
    try:
        # 检查 API 错误响应
        if "error" in data and isinstance(data.get("error"), (dict, str)):
            error_info = data["error"]
            error_msg = (
                error_info.get("message", str(error_info))
                if isinstance(error_info, dict)
                else str(error_info)
            )
            return {
                "ok": False,
                "error": "api_error",
                "content": error_msg,
                "sources": [],
                "raw": json.dumps(data, ensure_ascii=False)[:2000],
                "elapsed_ms": int((time.time() - started) * 1000),
            }

        choice0 = (data.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        message = msg.get("content") or ""
    except Exception:
        message = ""

    # 响应为空时返回失败
    if not message:
        return {
            "ok": False,
            "error": "empty_response",
            "content": "",
            "sources": [],
            "raw": json.dumps(data, ensure_ascii=False)[:2000] if data else "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

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
            for url_str in _extract_urls(content):
                sources.append({"url": url_str, "title": "", "snippet": ""})
    else:
        raw = message
        content = message
        for url_str in _extract_urls(message):
            sources.append({"url": url_str, "title": "", "snippet": ""})

    return {
        "ok": True,
        "content": content,
        "sources": sources,
        "raw": raw,
        "model": data.get("model") or model,
        "usage": data.get("usage") or {},
        "elapsed_ms": int((time.time() - started) * 1000),
    }
