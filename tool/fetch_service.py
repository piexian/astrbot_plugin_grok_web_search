"""网页抓取编排：配置解析、本地校验与 API 分发。

main.py（LLM Tool）与 public_api.py（SDK 门面）共用同一业务入口，
保证两个入口的拦截语义一致：Tool 继续展示字符串，SDK 获取完整字典。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config import parse_json_setting
from .tool import DEFAULT_MODEL

# 配置读取器签名：(key, default) -> 值
ConfigGetter = Callable[..., Any]


def _load_api():
    """延迟加载协议适配器；兼容插件包与 Skill 安装态两种包上下文。"""
    try:
        from ..api.grok_chat import grok_fetch
    except ImportError:
        from api.grok_chat import grok_fetch
    return grok_fetch


async def execute_fetch(get_cfg: ConfigGetter, url: str) -> dict[str, Any]:
    """抓取网页并返回完整结构化结果；本地校验失败返回结构化错误，不抛出。

    返回 dict：
    - ok/content/model/usage/elapsed_ms（成功，API 原有字段）
    - ok=False + error/error_kind=invalid_url|invalid_config（本地拦截）
    - ok=False + API 错误字段（失败，沿用作适配器的错误结构）
    """
    url = str(url or "")
    if not url or not url.startswith("http"):
        return {
            "ok": False,
            "error_kind": "invalid_url",
            "error": "请提供完整的 HTTP/HTTPS URL",
        }

    # 配置解析失败显式报错，不静默降级
    extra_body, body_error = parse_json_setting(get_cfg("extra_body", ""))
    extra_headers, headers_error = parse_json_setting(get_cfg("extra_headers", ""))
    config_error = body_error or headers_error
    if config_error:
        return {
            "ok": False,
            "error_kind": "invalid_config",
            "error": f"扩展参数配置无效（{config_error}），请检查插件设置",
        }

    timeout = get_cfg("timeout_seconds", 60)
    grok_fetch = _load_api()
    return await grok_fetch(
        url=url,
        base_url=get_cfg("base_url", ""),
        api_key=get_cfg("api_key", ""),
        model=get_cfg("model", DEFAULT_MODEL),
        timeout=float(timeout) if timeout else 60.0,
        extra_body=extra_body or None,
        extra_headers=extra_headers or None,
        proxy=str(get_cfg("proxy", "") or "") or None,
    )
