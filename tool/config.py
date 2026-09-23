"""插件配置映射、默认值与读取的唯一出口。

main.py 与 skill/scripts/grok_search.py 共用，保证分组/平铺兼容、
默认值和 extra_body / extra_headers 的 JSON 解析语义一致。
"""

from __future__ import annotations

from typing import Any

from .image_search import (
    DEFAULT_IMAGE_SEARCH_MAX_IMAGES,
    DEFAULT_IMAGE_SEARCH_TIMEOUT,
)
from .tool import DEFAULT_MODEL, parse_json_config

# 键 → (分组, 子键)；兼容旧版平铺配置。
CONFIG_PATHS: dict[str, tuple[str, str]] = {
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
    "serpapi_api_key": ("reverse_image_search", "serpapi_api_key"),
    "saucenao_api_key": ("reverse_image_search", "saucenao_api_key"),
    "image_search_timeout": ("reverse_image_search", "image_search_timeout"),
    "image_search_max_images": ("reverse_image_search", "image_search_max_images"),
}

CONFIG_DEFAULTS: dict[str, Any] = {
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
    "serpapi_api_key": "",
    "saucenao_api_key": "",
    "image_search_timeout": DEFAULT_IMAGE_SEARCH_TIMEOUT,
    "image_search_max_images": DEFAULT_IMAGE_SEARCH_MAX_IMAGES,
}


def config_value(config: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """优先读分组配置，回退平铺键，最后返回 default。"""
    path = CONFIG_PATHS.get(key)
    if path and config:
        section = config.get(path[0])
        if isinstance(section, dict) and path[1] in section:
            return section[path[1]]
    if config and key in config:
        return config[key]
    return default


def config_default(key: str) -> Any:
    """返回配置项的插件默认值。"""
    return CONFIG_DEFAULTS.get(key)


def parse_json_setting(value: Any) -> tuple[dict[str, Any], str | None]:
    """解析 extra_body / extra_headers 配置：dict 原样接受，JSON 文本统一解析。

    返回 (dict, 错误信息)；解析失败由调用方决定上报方式，本模块不做日志。
    """
    if isinstance(value, dict):
        return dict(value), None
    if isinstance(value, str):
        return parse_json_config(value)
    return {}, None


def migrate_legacy_config(config: dict[str, Any], save=None) -> bool:
    """把旧平铺配置值一次性搬进分组结构；返回是否有变更。"""
    changed = False
    for key, path in CONFIG_PATHS.items():
        if key not in config:
            continue

        default = CONFIG_DEFAULTS.get(key)
        legacy_value = config.get(key)
        if legacy_value == default:
            continue

        section = config.get(path[0])
        if not isinstance(section, dict):
            section = {}
            config[path[0]] = section

        current_value = section.get(path[1], default)
        if current_value != default:
            continue

        section[path[1]] = legacy_value
        config[key] = list(default) if isinstance(default, list) else default
        changed = True

    if changed and callable(save):
        save()
    return changed
