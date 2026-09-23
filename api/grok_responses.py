"""
Grok Responses API 异步客户端

通过 xAI Responses API (/v1/responses) 调用 Grok 进行真正的联网搜索
Responses API 支持 web_search 和 x_search 工具实现联网搜索

注意：此模块适用于直连 xAI 官方 API 的场景。
"""

import json
import time
from typing import Any

import aiohttp

try:  # 插件包上下文（相对导入）
    from ..tool.tool import (
        DEFAULT_JSON_SYSTEM_PROMPT,
        DEFAULT_MODEL,
        IMAGE_UNSUPPORTED_ERROR,
        build_headers,
        build_user_content,
        format_http_error,
        get_local_time_info,
        make_error_result,
        merge_citations_into_sources,
        merge_extra_body,
        normalize_base_url,
        parse_sources_from_message,
        retry_request,
        validate_config,
    )
except ImportError:  # Skill 安装态的顶层包上下文
    from tool.tool import (
        DEFAULT_JSON_SYSTEM_PROMPT,
        DEFAULT_MODEL,
        IMAGE_UNSUPPORTED_ERROR,
        build_headers,
        build_user_content,
        format_http_error,
        get_local_time_info,
        make_error_result,
        merge_citations_into_sources,
        merge_extra_body,
        normalize_base_url,
        parse_sources_from_message,
        retry_request,
        validate_config,
    )


async def grok_responses_search(
    query: str,
    base_url: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
    extra_body: dict | None = None,
    extra_headers: dict | None = None,
    system_prompt: str | None = None,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    retryable_status_codes: set[int] | None = None,
    images: list[str] | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """
    通过 xAI Responses API 进行联网搜索（异步）

    使用 /v1/responses 端点，支持 web_search 和 x_search 工具。
    仅适用于直连 xAI 官方 API 的场景。

    Args:
        query: 搜索查询内容
        base_url: xAI API 端点（如 https://api.x.ai）
        api_key: API 密钥
        model: 模型名称
        timeout: 超时时间（秒）
        extra_body: 额外请求体参数
        extra_headers: 额外请求头
        system_prompt: 自定义系统提示词
        max_retries: 最大重试次数
        retry_delay: 重试间隔时间（秒）
        retryable_status_codes: 可重试的 HTTP 状态码集合
        images: 可选的 base64 编码图片列表
        proxy: HTTP 代理地址

    Returns:
        {
            "ok": bool,
            "content": str,
            "sources": list,
            "raw": str,
            "error": str,
            "elapsed_ms": int,
            "retries": int,
            "citations": list,
        }
    """
    started = time.time()

    # 验证必要参数
    config = validate_config(base_url, api_key, started, base_url_label="xAI API 端点")
    if isinstance(config, dict):
        return config
    base_url, api_key = config

    # 使用 Responses API 端点
    url = f"{normalize_base_url(base_url)}/v1/responses"

    # 使用自定义提示词或默认提示词
    final_system_prompt = (
        system_prompt if system_prompt is not None else DEFAULT_JSON_SYSTEM_PROMPT
    )

    # 注入时间上下文
    time_context = get_local_time_info()
    enriched_query = f"{time_context}\n{query}"

    # 构建用户消息内容
    user_input = build_user_content(enriched_query, images, kind="responses")
    if user_input is IMAGE_UNSUPPORTED_ERROR:
        return IMAGE_UNSUPPORTED_ERROR

    # 构建 Responses API 请求体
    body: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": final_system_prompt},
            {"role": "user", "content": user_input},
        ],
        "tools": [
            {"type": "web_search"},
            {"type": "x_search"},
        ],
    }

    merge_extra_body(body, extra_body, {"model", "input", "tools", "stream"})
    headers = build_headers(api_key, extra_headers)

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

                try:
                    data = json.loads(raw_text)
                    return {"ok": True, "data": data}
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

    # 解析 Responses API 响应
    message = ""
    citations: list[dict[str, str]] = []
    usage_info = {}
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

        # Responses API 响应格式：
        # output 数组包含 web_search_call、reasoning、message 等元素
        output = data.get("output", [])
        if not output:
            parse_error = "响应缺少 output 字段"
        else:
            for item in output:
                if item.get("type") == "message":
                    content_list = item.get("content", [])
                    for content_item in content_list:
                        if content_item.get("type") == "output_text":
                            message = content_item.get("text", "")
                            annotations = content_item.get("annotations", [])
                            for ann in annotations:
                                if ann.get("type") == "url_citation":
                                    citations.append(
                                        {
                                            "url": ann.get("url", ""),
                                            "title": ann.get("title", ""),
                                        }
                                    )
                            break
                    break

            usage = data.get("usage", {})
            if usage:
                usage_info = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }

            # 提取顶层 citations（纯 URL 列表）
            top_citations = data.get("citations", [])
            if isinstance(top_citations, list):
                for url_str in top_citations:
                    if isinstance(url_str, str) and url_str.startswith("http"):
                        if not any(c.get("url") == url_str for c in citations):
                            citations.append({"url": url_str, "title": ""})

        if not message:
            parse_error = parse_error or "API 返回了空响应"

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

    # 解析 sources
    parsed_msg = parse_sources_from_message(message)
    sources = parsed_msg["sources"]

    # citations 内部按 URL 保序去重（annotations 与顶层 citations 可能重复）
    deduped_citations: list[dict[str, str]] = []
    seen_citation_urls: set[str] = set()
    for cit in citations:
        url = str(cit.get("url") or "").strip()
        if not url or url in seen_citation_urls:
            continue
        seen_citation_urls.add(url)
        deduped_citations.append(cit)
    citations = deduped_citations

    # 没有从 JSON 中提取到 sources 时，统一出口合并 API citations（按 URL 保序去重）
    if not sources:
        sources = merge_citations_into_sources(sources, citations)
    return {
        "ok": True,
        "content": parsed_msg["content"],
        "sources": sources,
        "raw": parsed_msg["raw"],
        "model": data.get("model") or model,
        "usage": usage_info,
        "elapsed_ms": int((time.time() - started) * 1000),
        "retries": retry_count,
        "citations": citations,
    }
