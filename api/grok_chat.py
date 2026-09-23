"""
Grok Chat Completions API 异步客户端

通过 OpenAI 兼容接口 (/v1/chat/completions) 调用 Grok 进行联网搜索。
"""

import json
import time
from typing import Any

import aiohttp

try:  # 插件包上下文（相对导入）
    from ..tool.tool import (
        DEFAULT_JSON_SYSTEM_PROMPT,
        DEFAULT_MODEL,
        FETCH_SYSTEM_PROMPT,
        IMAGE_UNSUPPORTED_ERROR,
        build_headers,
        build_user_content,
        format_http_error,
        get_local_time_info,
        make_error_result,
        merge_extra_body,
        normalize_base_url,
        parse_sources_from_message,
        retry_request,
        strip_stream_decorations,
        validate_config,
    )
except ImportError:  # Skill 安装态的顶层包上下文
    from tool.tool import (
        DEFAULT_JSON_SYSTEM_PROMPT,
        DEFAULT_MODEL,
        FETCH_SYSTEM_PROMPT,
        IMAGE_UNSUPPORTED_ERROR,
        build_headers,
        build_user_content,
        format_http_error,
        get_local_time_info,
        make_error_result,
        merge_extra_body,
        normalize_base_url,
        parse_sources_from_message,
        retry_request,
        strip_stream_decorations,
        validate_config,
    )


async def grok_search(
    query: str,
    base_url: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
    reasoning_effort: str | None = None,
    reasoning_budget_tokens: int | None = None,
    extra_body: dict | None = None,
    extra_headers: dict | None = None,
    stream: bool = False,
    system_prompt: str | None = None,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    retryable_status_codes: set[int] | None = None,
    images: list[str] | None = None,
    proxy: str | None = None,
    parse_json_response: bool = True,
) -> dict[str, Any]:
    """
    调用 Grok API 进行联网搜索（异步）

    Args:
        query: 搜索查询内容
        base_url: Grok API 端点
        api_key: API 密钥
        model: 模型名称
        timeout: 超时时间（秒）
        reasoning_effort: 思考模式强度，None 不开启 / "medium" / "high"
        reasoning_budget_tokens: 思考 token 预算
        extra_body: 额外请求体参数
        extra_headers: 额外请求头
        system_prompt: 自定义系统提示词，为 None 时使用默认提示词
        max_retries: 最大重试次数（默认 3 次）
        retry_delay: 重试间隔时间（秒，默认 1.0）
        retryable_status_codes: 可重试的 HTTP 状态码集合，为 None 时使用默认值
        images: 可选的 base64 编码图片列表，用于构建多模态消息
        proxy: HTTP 代理地址
        parse_json_response: 搜索时解析 JSON；抓取时保留完整 Markdown 正文

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
    config = validate_config(base_url, api_key, started, base_url_label="Grok API 端点")
    if isinstance(config, dict):
        return config
    base_url, api_key = config

    url = f"{normalize_base_url(base_url)}/v1/chat/completions"

    # 使用自定义提示词或默认提示词
    final_system_prompt = (
        system_prompt if system_prompt is not None else DEFAULT_JSON_SYSTEM_PROMPT
    )

    # 注入时间上下文
    time_context = get_local_time_info()
    enriched_query = f"{time_context}\n{query}"

    # 构建用户消息：如果有图片则使用多模态格式
    user_content = build_user_content(enriched_query, images, kind="chat")
    if user_content is IMAGE_UNSUPPORTED_ERROR:
        return IMAGE_UNSUPPORTED_ERROR
    user_message: dict[str, Any] = {"role": "user", "content": user_content}

    # 构建请求体
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": final_system_prompt},
            user_message,
        ],
        "temperature": 0.2,
        "stream": stream,
    }
    if model:
        body["model"] = model

    # 添加思考模式参数
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
        if reasoning_budget_tokens:
            body["reasoning_budget_tokens"] = reasoning_budget_tokens

    merge_extra_body(
        body,
        extra_body,
        {"model", "messages", "stream", "reasoning_effort", "reasoning_budget_tokens"},
    )
    headers = build_headers(api_key, extra_headers)

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

    async with aiohttp.ClientSession() as session:

        async def _do_request(req_proxy: str | None = None) -> dict[str, Any]:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                proxy=req_proxy,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return format_http_error(
                        resp.status, error_text, started, resp.headers
                    )

                raw_text = await resp.text()
                content_type = resp.headers.get("Content-Type", "")

                # 检查是否为 SSE 流式响应
                is_sse = (
                    "text/event-stream" in content_type
                    or raw_text.strip().startswith("data:")
                )

                if is_sse:
                    parsed = _parse_sse_response(raw_text)
                    if parsed:
                        return {"ok": True, "data": parsed}
                    return make_error_result(
                        "SSE 流式响应解析失败",
                        started,
                        raw=raw_text[:2000] if raw_text else "",
                    )

                try:
                    return {"ok": True, "data": json.loads(raw_text)}
                except json.JSONDecodeError:
                    return make_error_result(
                        "响应解析失败，API 返回了非 JSON 格式的数据",
                        started,
                        raw=raw_text[:2000] if raw_text else "",
                    )

        # 使用通用重试执行器
        result = await retry_request(
            _do_request,
            proxy=proxy,
            max_retries=max_retries,
            retry_delay=retry_delay,
            retryable_status_codes=retryable_status_codes,
            timeout=timeout,
            started=started,
        )

    if not result.get("ok") or "data" not in result:
        return result
    data = result["data"]
    retry_count = result.get("retries", 0)

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
            return make_error_result(
                f"API 返回错误: {error_msg}",
                started,
                raw=json.dumps(data, ensure_ascii=False)[:2000],
                kind="api",
            )

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

    if not message:
        error_detail = parse_error or "API 返回了空响应"
        return make_error_result(
            f"{error_detail}，请稍后重试",
            started,
            retry_count,
            raw=json.dumps(data, ensure_ascii=False)[:2000] if data else "",
            kind="empty",
        )

    if parse_json_response:
        parsed_msg = parse_sources_from_message(message)
    else:
        # 网页中的 JSON 示例不是搜索协议，不能交给 JSON 提取器。
        parsed_msg = {
            "content": strip_stream_decorations(message),
            "sources": [],
            "raw": "",
        }

    return {
        "ok": True,
        "content": parsed_msg["content"],
        "sources": parsed_msg["sources"],
        "raw": parsed_msg["raw"],
        "model": data.get("model") or model,
        "usage": data.get("usage") or {},
        "elapsed_ms": int((time.time() - started) * 1000),
        "retries": retry_count,
    }


async def grok_fetch(
    url: str,
    base_url: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
    extra_body: dict | None = None,
    extra_headers: dict | None = None,
    proxy: str | None = None,
    max_retries: int = 2,
) -> dict[str, Any]:
    """利用 Grok 联网能力抓取指定 URL 的网页内容并转为 Markdown

    Args:
        url: 要抓取的网页 URL
        base_url: Grok API 端点
        api_key: API 密钥
        model: 模型名称
        timeout: 超时时间（秒）
        extra_body: 额外请求体参数
        extra_headers: 额外请求头
        proxy: 代理地址

    Returns:
        {
            "ok": bool,
            "content": str,      # Markdown 格式的网页内容
            "error": str,        # 错误信息（失败时）
            "elapsed_ms": int,
        }
    """
    result = await grok_search(
        query=f"{url}\n获取该网页内容并返回其结构化 Markdown 格式",
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        reasoning_effort=None,
        reasoning_budget_tokens=None,
        extra_body=extra_body,
        extra_headers=extra_headers,
        system_prompt=FETCH_SYSTEM_PROMPT,
        max_retries=max_retries,
        proxy=proxy,
        parse_json_response=False,
    )

    if not result.get("ok"):
        return result

    content = result.get("content", "")

    return {
        "ok": True,
        "content": content,
        "elapsed_ms": result.get("elapsed_ms", 0),
    }
