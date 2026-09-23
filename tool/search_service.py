"""搜索编排：选项规范化、模式/模型/提示词/重试解析与 API 分发。

main.py（指令 / LLM Tool）与 skill 脚本共用；Chat 与 Responses 协议差异
留在 api/ 各自适配器中，这里只做统一入口。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config import parse_json_setting
from .tool import (
    DEFAULT_JSON_SYSTEM_PROMPT,
    DEFAULT_MODEL,
    build_search_query,
    build_search_time_constraints,
    normalize_search_options,
    resolve_mode_model,
    resolve_reasoning_params,
    resolve_search_mode,
    resolve_system_prompt,
    safe_number,
)

# 配置读取器签名：(key, default) -> 值
ConfigGetter = Callable[..., Any]


def resolve_model(get_cfg: ConfigGetter, mode: str, explicit_model: str = "") -> str:
    """模型优先级：单次显式覆盖 > 模式专用模型 > 全局 model > 默认。"""
    if explicit_model:
        return explicit_model
    mode_model = str(get_cfg(f"{mode}_model", "") or "")
    fallback = str(get_cfg("model", "") or DEFAULT_MODEL)
    return resolve_mode_model(mode_model, fallback)


def resolve_retry_params(
    get_cfg: ConfigGetter, use_retry: bool
) -> tuple[int, float, set[int] | None]:
    """重试参数：仅 /grok 指令启用；LLM Tool 与 Skill 默认不自动重试。"""
    if not use_retry:
        return 0, 1.0, None
    max_retries = get_cfg("max_retries", 3)
    retry_delay = get_cfg("retry_delay", 1.0)
    retryable: set[int] | None = None
    codes = get_cfg("retryable_status_codes", [])
    if codes and isinstance(codes, list):
        retryable = set(codes)
    return max_retries, retry_delay, retryable


def _load_api():
    """延迟加载协议适配器；兼容插件包与 Skill 安装态两种包上下文。"""
    try:
        from ..api.grok_chat import grok_fetch, grok_search
        from ..api.grok_responses import grok_responses_search
    except ImportError:
        from api.grok_chat import grok_fetch, grok_search
        from api.grok_responses import grok_responses_search
    return grok_search, grok_responses_search, grok_fetch


async def execute_search(
    get_cfg: ConfigGetter,
    query: str,
    *,
    system_prompt: str | None = None,
    use_retry: bool = False,
    images: list[str] | None = None,
    search_depth: str = "basic",
    max_results: int = 7,
    topic: str = "general",
    days: int = 0,
    time_range: str = "",
    start_date: str = "",
    end_date: str = "",
    explicit_model: str = "",
) -> dict[str, Any]:
    """执行一次搜索并返回 API 结果字典；网络异常向上抛出由入口分类。"""
    opts = normalize_search_options(
        search_depth=search_depth,
        max_results=max_results,
        topic=topic,
        days=days,
        time_range=time_range,
        start_date=start_date,
        end_date=end_date,
    )
    depth = str(opts["search_depth"])

    timeout = safe_number(
        get_cfg("timeout_seconds", 60), 60.0, cast=float, min_val=0.001
    )
    model = resolve_model(get_cfg, resolve_search_mode(depth), explicit_model)
    reasoning_effort, reasoning_budget_tokens = resolve_reasoning_params(depth)
    max_retries, retry_delay, retryable_codes = resolve_retry_params(get_cfg, use_retry)
    if system_prompt is None:
        system_prompt = resolve_system_prompt(
            get_cfg("custom_system_prompt", ""), DEFAULT_JSON_SYSTEM_PROMPT
        )

    time_constraints = build_search_time_constraints(
        topic=str(opts["topic"]),
        days=int(opts["days"]),
        time_range=str(opts["time_range"]),
        start_date=str(opts["start_date"]),
        end_date=str(opts["end_date"]),
    )
    enriched_query = build_search_query(
        query, depth, int(opts["max_results"]), time_constraints
    )

    # 配置解析失败显式抛错，由入口上报，不做静默降级
    extra_body, body_error = parse_json_setting(get_cfg("extra_body", ""))
    if body_error:
        raise ValueError(f"extra_body {body_error}")
    extra_headers, headers_error = parse_json_setting(get_cfg("extra_headers", ""))
    if headers_error:
        raise ValueError(f"extra_headers {headers_error}")

    grok_search, grok_responses_search, _ = _load_api()
    common_kwargs: dict[str, Any] = {
        "query": enriched_query,
        "base_url": get_cfg("base_url", ""),
        "api_key": get_cfg("api_key", ""),
        "model": model,
        "timeout": timeout,
        "extra_body": extra_body,
        "extra_headers": extra_headers,
        "system_prompt": system_prompt,
        "max_retries": max_retries,
        "retry_delay": retry_delay,
        "retryable_status_codes": retryable_codes,
        "images": images,
        "proxy": str(get_cfg("proxy", "") or "").strip() or None,
    }

    if get_cfg("use_responses_api", False):
        return await grok_responses_search(**common_kwargs)
    return await grok_search(
        reasoning_effort=reasoning_effort,
        reasoning_budget_tokens=reasoning_budget_tokens,
        stream=bool(get_cfg("enable_stream", False)),
        **common_kwargs,
    )
