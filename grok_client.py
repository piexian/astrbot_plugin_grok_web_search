"""
Grok API 异步客户端

通过 OpenAI 兼容接口调用 Grok 进行联网搜索
"""

import asyncio
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


async def grok_search(
    query: str,
    base_url: str,
    api_key: str,
    model: str = "grok-4-fast",
    timeout: float = 60.0,
    enable_thinking: bool = True,
    thinking_budget: int = 32000,
    extra_body: dict | None = None,
    extra_headers: dict | None = None,
    session: aiohttp.ClientSession | None = None,
    system_prompt: str | None = None,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    retryable_status_codes: set[int] | None = None,
) -> dict[str, Any]:
    """
    调用 Grok API 进行联网搜索（异步）

    Args:
        query: 搜索查询内容
        base_url: Grok API 端点
        api_key: API 密钥
        model: 模型名称
        timeout: 超时时间（秒）
        enable_thinking: 是否开启思考模式
        thinking_budget: 思考 token 预算
        extra_body: 额外请求体参数
        extra_headers: 额外请求头
        session: 可选的 aiohttp.ClientSession，传入时复用，否则创建临时 session
        system_prompt: 自定义系统提示词，为 None 时使用默认提示词
        max_retries: 最大重试次数（默认 3 次）
        retry_delay: 重试间隔时间（秒，默认 1.0）
        retryable_status_codes: 可重试的 HTTP 状态码集合，为 None 时使用默认值

    Returns:
        {
            "ok": bool,
            "content": str,      # 综合答案
            "sources": list,     # 来源列表 [{url, title, snippet}]
            "raw": str,          # 原始响应（解析失败时）
            "error": str,        # 错误信息（失败时）
            "elapsed_ms": int,   # 耗时
            "retries": int,      # 重试次数
        }
    """
    started = time.time()

    # 验证必要参数
    base_url = _normalize_base_url_value(base_url)
    api_key = normalize_api_key(api_key)

    if not base_url:
        return {
            "ok": False,
            "error": "缺少 base_url 配置，请在插件设置中填写 Grok API 端点",
            "content": "",
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    if not api_key:
        return {
            "ok": False,
            "error": "缺少 api_key 配置，请在插件设置中填写 API 密钥",
            "content": "",
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    url = f"{normalize_base_url(base_url)}/v1/chat/completions"

    # 默认系统提示词（LLM Tool 和 Skill 使用）
    default_system_prompt = (
        "You are a web research assistant. Use live web search/browsing when answering. "
        "Return ONLY a single JSON object with keys: "
        "content (string), sources (array of objects with url/title/snippet when possible). "
        "Keep content concise and evidence-backed. "
        "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
    )

    # 使用自定义提示词或默认提示词
    final_system_prompt = (
        system_prompt if system_prompt is not None else default_system_prompt
    )

    # 构建请求体：仅当提供 model 时才添加 model 字段（允许供应商/端点使用默认模型）
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": final_system_prompt},
            {"role": "user", "content": query},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    if model:
        body["model"] = model

    # 添加思考模式参数
    if enable_thinking:
        body["reasoning_effort"] = "high"
        if thinking_budget > 0:
            body["reasoning_budget_tokens"] = thinking_budget

    if extra_body:
        # 保护关键字段不被覆盖
        protected_keys = {"model", "messages", "stream"}
        for key, value in extra_body.items():
            if key not in protected_keys:
                body[key] = value

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if extra_headers:
        # 保护关键请求头不被覆盖
        protected_headers = {"authorization", "content-type"}
        for key, value in extra_headers.items():
            if str(key).lower() not in protected_headers:
                headers[str(key)] = str(value)

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

    async def _do_request(
        s: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        async with s.post(
            url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                # 友好的错误提示
                error_hints = {
                    400: "请求格式错误，请检查 extra_body 配置",
                    401: "认证失败，请检查 api_key 是否正确",
                    403: "访问被拒绝，请检查 API 权限",
                    404: "API 端点不存在，请检查 base_url 配置",
                    429: "请求过于频繁，请稍后重试",
                    500: "服务器内部错误",
                    502: "网关错误，API 服务可能暂时不可用",
                    503: "服务暂时不可用，请稍后重试",
                }
                hint = error_hints.get(resp.status, "")
                error_msg = f"HTTP {resp.status}"
                if hint:
                    error_msg = f"{error_msg} - {hint}"
                return {
                    "ok": False,
                    "error": error_msg,
                    "content": "",
                    "sources": [],
                    "raw": error_text[:2000] if error_text else "",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

            # 读取响应内容
            raw_text = await resp.text()
            content_type = resp.headers.get("Content-Type", "")

            # 检查是否为 SSE 流式响应
            is_sse = "text/event-stream" in content_type or raw_text.strip().startswith(
                "data:"
            )

            if is_sse:
                # 解析 SSE 流式响应
                parsed = _parse_sse_response(raw_text)
                if parsed:
                    return {"ok": True, "data": parsed}
                return {
                    "ok": False,
                    "error": "SSE 流式响应解析失败",
                    "content": "",
                    "sources": [],
                    "raw": raw_text[:2000] if raw_text else "",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

            # 尝试解析 JSON 响应
            try:
                return {"ok": True, "data": json.loads(raw_text)}
            except json.JSONDecodeError as e:
                return {
                    "ok": False,
                    "error": "响应解析失败，API 返回了非 JSON 格式的数据",
                    "content": str(e),
                    "sources": [],
                    "raw": raw_text[:2000] if raw_text else "",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

    # 可重试的错误状态码（默认值）
    if retryable_status_codes is None:
        retryable_status_codes = {429, 500, 502, 503, 504}

    result = None
    last_error = None
    retry_count = 0

    for attempt in range(max_retries + 1):
        try:
            if session is not None:
                result = await _do_request(session)
            else:
                async with aiohttp.ClientSession() as temp_session:
                    result = await _do_request(temp_session)

            # 检查是否需要重试
            if result.get("ok") or "data" in result:
                # 成功或解析成功的响应，跳出循环
                break

            # 检查是否为可重试的错误
            error_msg = result.get("error", "")
            should_retry = False

            # HTTP 状态码可重试
            if any(f"HTTP {code}" in error_msg for code in retryable_status_codes):
                should_retry = True

            if should_retry and attempt < max_retries:
                retry_count = attempt + 1
                await asyncio.sleep(retry_delay * (attempt + 1))  # 指数退避
                continue

            # 不可重试的错误，直接返回
            break

        except aiohttp.ClientError as e:
            last_error = f"网络请求失败: {e}"
            if attempt < max_retries:
                retry_count = attempt + 1
                await asyncio.sleep(retry_delay * (attempt + 1))
                continue
            return {
                "ok": False,
                "error": last_error,
                "content": "",
                "sources": [],
                "raw": "",
                "elapsed_ms": int((time.time() - started) * 1000),
                "retries": retry_count,
            }
        except TimeoutError:
            last_error = (
                f"请求超时（{timeout}秒），请检查网络或增加 timeout_seconds 配置"
            )
            if attempt < max_retries:
                retry_count = attempt + 1
                await asyncio.sleep(retry_delay * (attempt + 1))
                continue
            return {
                "ok": False,
                "error": last_error,
                "content": "",
                "sources": [],
                "raw": "",
                "elapsed_ms": int((time.time() - started) * 1000),
                "retries": retry_count,
            }

    if result is None:
        return {
            "ok": False,
            "error": last_error or "未知错误",
            "content": "",
            "sources": [],
            "raw": "",
            "elapsed_ms": int((time.time() - started) * 1000),
            "retries": retry_count,
        }

    if not result.get("ok") or "data" not in result:
        result["retries"] = retry_count
        return result
    data = result["data"]

    # 解析响应
    message = ""
    parse_error = ""
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
                "error": f"API 返回错误: {error_msg}",
                "content": "",
                "sources": [],
                "raw": json.dumps(data, ensure_ascii=False)[:2000],
                "elapsed_ms": int((time.time() - started) * 1000),
            }

        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            parse_error = f"响应缺少 choices 字段或格式异常: {type(choices).__name__}"
        else:
            choice0 = choices[0] if choices else {}
            msg = choice0.get("message") or {}
            message = msg.get("content") or ""
            if not message:
                parse_error = "choices[0].message.content 为空"
    except (KeyError, IndexError, TypeError) as e:
        parse_error = f"响应结构解析失败: {type(e).__name__}: {e}"

    # 响应为空时返回失败
    if not message:
        error_detail = parse_error or "API 返回了空响应"
        return {
            "ok": False,
            "error": f"{error_detail}，请稍后重试",
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
        "retries": retry_count,
    }
