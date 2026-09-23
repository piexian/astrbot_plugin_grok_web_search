#!/usr/bin/env python3
"""Grok 搜索 / 网页读取 / 反向搜图 Skill CLI。

不维护第二份协议实现：HTTP/SSE/来源解析/重试语义直接复用插件共享核心
（tool/ 与 api/ 代码包），安装态与仓库开发态均可运行。
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from typing import Any

# ─── 共享核心导入路径：安装态 (skill 根) 与仓库开发态 (skill 上级) ───
_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_ROOT = os.path.dirname(_HERE)
_PLUGIN_ROOT = os.path.dirname(_SKILL_ROOT)
for _path in (_PLUGIN_ROOT, _SKILL_ROOT):
    if _path and os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

from tool.config import config_value, parse_json_setting  # noqa: E402
from tool.image_search import (  # noqa: E402
    DEFAULT_IMAGE_SEARCH_MAX_IMAGES,
    DEFAULT_IMAGE_SEARCH_TIMEOUT,
    format_evidence,
    run_reverse_image_search,
)
from tool.search_service import _load_api, execute_search, resolve_model  # noqa: E402
from tool.tool import (  # noqa: E402
    DEFAULT_MODEL,
    normalize_api_key,
    normalize_base_url,
    normalize_search_options,
    resolve_search_mode,
)

# 与 main.py CONFIG_PATHS 一致的分组读取由共享 tool.config 提供


def _load_image_search_adapters():
    """懒加载反向搜图适配层（依赖 aiohttp），缺失时抛 ImportError。"""
    from api.saucenao import saucenao_search
    from api.serpapi_lens import serpapi_lens_search

    return serpapi_lens_search, saucenao_search


def _run_reverse_image_search_sync(
    args: argparse.Namespace, config: dict[str, Any], images: list[str]
) -> dict[str, Any]:
    """同步执行反向搜图（含本地拦截），返回聚合结果。"""
    use_serpapi = args.serpapi or args.all
    use_saucenao = args.saucenao or args.all
    if not (use_serpapi or use_saucenao):
        return {}
    empty: dict[str, Any] = {
        "requested": True,
        "serpapi": {"ok": False, "matches": [], "error": ""},
        "saucenao": {"ok": False, "matches": [], "error": ""},
        "notes": [],
        "evidence_text": "",
    }
    try:
        serpapi_fn, saucenao_fn = _load_image_search_adapters()
    except ImportError as e:
        empty["notes"] = [f"反向搜图依赖不可用，已跳过（未产生搜图请求）: {e}"]
        empty["evidence_text"] = format_evidence(empty)
        return empty
    try:
        timeout = float(
            config_value(config, "image_search_timeout") or DEFAULT_IMAGE_SEARCH_TIMEOUT
        )
    except (TypeError, ValueError):
        timeout = float(DEFAULT_IMAGE_SEARCH_TIMEOUT)
    if timeout <= 0:
        timeout = float(DEFAULT_IMAGE_SEARCH_TIMEOUT)
    try:
        max_images = int(
            config_value(config, "image_search_max_images")
            or DEFAULT_IMAGE_SEARCH_MAX_IMAGES
        )
    except (TypeError, ValueError):
        max_images = DEFAULT_IMAGE_SEARCH_MAX_IMAGES
    if max_images <= 0:
        max_images = DEFAULT_IMAGE_SEARCH_MAX_IMAGES
    return asyncio.run(
        run_reverse_image_search(
            images,
            use_serpapi=use_serpapi,
            use_saucenao=use_saucenao,
            serpapi_key=str(config_value(config, "serpapi_api_key") or ""),
            saucenao_key=str(config_value(config, "saucenao_api_key") or ""),
            timeout=timeout,
            proxy=str(config_value(config, "proxy") or ""),
            max_images=max_images,
            serpapi_fn=serpapi_fn,
            saucenao_fn=saucenao_fn,
        )
    )


def _compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _default_user_config_path() -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, ".codex", "config", "grok-search.json")


def _skill_root() -> str:
    return _SKILL_ROOT


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
    """配置候选：安装态 skill 根优先，其次插件持久化 skill 目录。"""
    root = _skill_root()
    paths = [
        os.path.join(root, "config.json"),
        os.path.join(root, "config.local.json"),
    ]
    data_path = _find_astrbot_data_path()
    if data_path:
        persistent = os.path.join(
            data_path, "plugin_data", "astrbot_plugin_grok_web_search", "skill"
        )
        paths += [
            os.path.join(persistent, "config.json"),
            os.path.join(persistent, "config.local.json"),
        ]
    return paths


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


async def _run_search(get_cfg, *, query: str, **kwargs) -> dict[str, Any]:
    """搜索入口：走插件共享编排（含超时/模型/提示词/重试与代理语义）。"""
    return await execute_search(get_cfg, query, use_retry=False, **kwargs)


async def _run_fetch(get_cfg, *, url: str, model: str) -> dict[str, Any]:
    """网页读取入口：始终走 Chat 协议与独立抓取提示词，不自动重试。"""
    _, _, grok_fetch = _load_api()
    return await grok_fetch(
        url=url,
        base_url=get_cfg("base_url", ""),
        api_key=get_cfg("api_key", ""),
        model=model,
        timeout=float(get_cfg("timeout_seconds", 60) or 60.0),
        extra_body=get_cfg("extra_body", "") or None,
        extra_headers=get_cfg("extra_headers", "") or None,
        proxy=str(get_cfg("proxy", "") or "").strip() or None,
        max_retries=0,
    )


def _write_output(result: dict[str, Any], mode: str, evidence_text: str = "") -> None:
    """LLM 输出仅保留证据字段，默认 JSON 输出保持兼容。"""
    if mode == "llm":
        result = {
            key: result[key]
            for key in ("ok", "content", "sources", "fetch_url", "error")
            if key in result
        }
        if evidence_text:
            result["evidence"] = evidence_text
    sys.stdout.write(_compact_json(result))


def _error_output(
    *,
    error: str,
    detail: str,
    config_path: str,
    config_status: str,
    model: str,
    started: float,
) -> dict[str, Any]:
    """旧诊断字段兼容的错误输出；LLM 输出只保留类别。"""
    return {
        "ok": False,
        "error": error,
        "detail": detail,
        "config_path": config_path,
        "config_status": config_status if config_status else "OK",
        "model": model,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _categorize_failure(result: dict[str, Any]) -> tuple[str, str]:
    """把共享结果字典映射回旧诊断类别（HTTP <code>/api_error/...）。"""
    status = result.get("status")
    if isinstance(status, int):
        return f"HTTP {status}", str(result.get("raw") or result.get("error") or "")
    kind = result.get("error_kind")
    if kind == "api":
        return "api_error", str(result.get("error") or "")
    if kind == "empty":
        return "empty_response", str(result.get("error") or "")
    return "request_failed", str(result.get("error") or "")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Web research, webpage reading and image-source lookup via Grok."
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
        "--depth",
        dest="search_depth",
        type=str,
        default="",
        help="basic: fact check; advanced: multi-part research; deep: complex/conflicting evidence.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=0,
        help="Target source count (5-20), not a guaranteed number of results.",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default="",
        help="general or news; news without time options defaults to the last 7 days.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Look back 1-365 days for either topic; overridden by time-range or explicit dates.",
    )
    parser.add_argument(
        "--time-range",
        type=str,
        default="",
        help="Research window: day (today), week (7d), month (30d), year (365d); dates take precedence.",
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
        help="Read accessible webpage content as Markdown; may fail or be partial.",
    )
    parser.add_argument(
        "--serpapi",
        action="store_true",
        help="Find matching images, source pages or product/place clues with Google Lens.",
    )
    parser.add_argument(
        "--saucenao",
        action="store_true",
        help="Find original artwork, artists or anime/manga sources with SauceNAO.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Enable both reverse image backends and force deep search depth.",
    )
    parser.add_argument(
        "--output",
        choices=("json", "llm"),
        default="json",
        help="Output mode: llm keeps evidence only; json preserves diagnostic fields.",
    )
    args = parser.parse_args()

    # 本地参数检查：fetch 模式与反向搜图互斥
    if (args.serpapi or args.saucenao or args.all) and args.fetch_url:
        sys.stderr.write(
            "Error: --fetch-url cannot be combined with --serpapi/--saucenao/--all\n"
        )
        return 2

    env_config_path = os.environ.get("GROK_CONFIG_PATH", "").strip()
    explicit_config_path = args.config.strip() or env_config_path

    config_path = ""
    config: dict[str, Any] = {}
    astrbot_config_status = ""

    # 优先尝试加载 AstrBot 插件配置
    astrbot_config, astrbot_config_status = _load_astrbot_plugin_config()
    if astrbot_config and normalize_api_key(
        str(config_value(astrbot_config, "api_key") or "")
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

            candidate_key = normalize_api_key(
                str(config_value(candidate_config, "api_key") or "")
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

    base_url = normalize_base_url(
        args.base_url.strip()
        or os.environ.get("GROK_BASE_URL", "").strip()
        or str(config_value(config, "base_url") or "").strip()
    )
    api_key = normalize_api_key(
        args.api_key.strip()
        or os.environ.get("GROK_API_KEY", "").strip()
        or str(config_value(config, "api_key") or "").strip()
    )
    # 显式模型（CLI/env）优先于模式与全局配置；未指定时按模式解析
    explicit_model = (
        args.model.strip() or os.environ.get("GROK_MODEL", "").strip()
    ).strip()

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
    if args.all:
        search_depth = "deep"  # --all 固定深度搜索
    max_results = int(str(opts["max_results"]))
    topic = str(opts["topic"])
    days = int(str(opts["days"]))
    time_range = str(opts["time_range"])
    start_date = str(opts["start_date"])
    end_date = str(opts["end_date"])

    timeout_seconds = args.timeout_seconds
    if not timeout_seconds:
        try:
            timeout_seconds = float(os.environ.get("GROK_TIMEOUT_SECONDS", "0") or "0")
        except (ValueError, TypeError):
            timeout_seconds = 0.0
    if not timeout_seconds:
        try:
            timeout_seconds = float(config_value(config, "timeout_seconds") or 0)
        except (ValueError, TypeError):
            timeout_seconds = 0.0
    if not timeout_seconds or timeout_seconds <= 0:
        timeout_seconds = 60.0

    # Responses API 开关由共享编排读取（use_responses_api 仅作用于搜索模式，
    # fetch 始终走 Chat 协议与独立解析分支）。

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
        # 插件 extra_body/extra_headers 支持 dict 与 JSON 文本两种配置形态（共享语义），
        # env / CLI 单次覆盖按优先级合并，不回写插件配置。
        extra_body, body_error = parse_json_setting(
            config_value(config, "extra_body", "")
        )
        if body_error:
            sys.stderr.write(f"Invalid extra_body config: {body_error}\n")
            return 2
        extra_body.update(_load_json_env("GROK_EXTRA_BODY_JSON"))
        extra_body.update(
            _parse_json_object(args.extra_body_json, label="--extra-body-json")
        )

        extra_headers, headers_error = parse_json_setting(
            config_value(config, "extra_headers", "")
        )
        if headers_error:
            sys.stderr.write(f"Invalid extra_headers config: {headers_error}\n")
            return 2
        extra_headers.update(_load_json_env("GROK_EXTRA_HEADERS_JSON"))
        extra_headers.update(
            _parse_json_object(args.extra_headers_json, label="--extra-headers-json")
        )
    except Exception as e:
        sys.stderr.write(f"Invalid JSON: {e}\n")
        return 2

    # 单次请求覆盖注入共享配置读取器（CLI/env 覆盖与扩展参数；插件配置仍作为兜底来源）
    overrides: dict[str, Any] = {
        "base_url": base_url,
        "api_key": api_key,
        "timeout_seconds": timeout_seconds,
        "extra_body": extra_body,
        "extra_headers": extra_headers,
    }

    def get_cfg(key: str, default: Any = None) -> Any:
        if key in overrides:
            return overrides[key]
        return config_value(config, key, default)

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

    # 判断运行模式：fetch 模式 vs search 模式；必填参数校验先于任何付费搜图请求
    fetch_url = args.fetch_url.strip() if hasattr(args, "fetch_url") else ""
    is_fetch_mode = bool(fetch_url)

    if is_fetch_mode:
        if not fetch_url.startswith("http"):
            sys.stderr.write("Error: --fetch-url must be a full HTTP/HTTPS URL\n")
            return 2
    elif not args.query:
        sys.stderr.write(
            "Error: --query is required (or use --fetch-url for fetch mode)\n"
        )
        return 2

    # 反向搜图（--serpapi/--saucenao/--all）；无有效图片时本地拦截，不产生任何搜图请求
    reverse_agg: dict[str, Any] = _run_reverse_image_search_sync(args, config, images)
    evidence_text = str(reverse_agg.get("evidence_text") or "") if reverse_agg else ""

    if is_fetch_mode:
        # fetch 模式使用全局模型（显式覆盖优先），不套用模式模型
        model = (
            explicit_model
            or str(config_value(config, "model") or "").strip()
            or DEFAULT_MODEL
        )
    else:
        model = resolve_model(
            get_cfg, resolve_search_mode(search_depth), explicit_model
        )

    def _emit_failure(error: str, detail: str) -> None:
        _write_output(
            _error_output(
                error=error,
                detail=detail,
                config_path=config_path,
                config_status=astrbot_config_status,
                model=model,
                started=started,
            ),
            args.output,
            evidence_text,
        )

    try:
        if is_fetch_mode:
            result = asyncio.run(_run_fetch(get_cfg, url=fetch_url, model=model))
        else:
            query = args.query
            # 反向搜图证据附加在查询末尾，供 Grok 核验；失败/跳过说明一并提供
            if evidence_text:
                query = f"{query}\n\n{evidence_text}"
            result = asyncio.run(
                _run_search(
                    get_cfg,
                    query=query,
                    images=images or None,
                    search_depth=search_depth,
                    max_results=max_results,
                    topic=topic,
                    days=days,
                    time_range=time_range,
                    start_date=start_date,
                    end_date=end_date,
                    explicit_model=explicit_model,
                )
            )
    except ImportError as e:
        _emit_failure(
            "request_failed",
            f"缺少运行依赖（宿主环境通常已内置）: {e}",
        )
        return 1
    except Exception as e:
        _emit_failure("request_failed", str(e))
        return 1

    if not result.get("ok"):
        error, detail = _categorize_failure(result)
        _emit_failure(error, detail)
        return 1

    message_empty = not str(result.get("content") or "")
    if message_empty:
        _emit_failure("empty_response", "API 返回空内容")
        return 1

    if is_fetch_mode:
        out: dict[str, Any] = {
            "ok": True,
            "fetch_url": fetch_url,
            "config_path": config_path,
            "model": result.get("model") or model,
            "content": result.get("content", ""),
            "usage": result.get("usage") or {},
            "elapsed_ms": result.get("elapsed_ms", 0),
        }
        _write_output(out, args.output)
        return 0

    out = {
        "ok": True,
        "query": args.query or fetch_url,
        "config_path": config_path,
        "model": result.get("model") or model,
        "content": result.get("content", ""),
        "sources": result.get("sources", []),
        "raw": result.get("raw", ""),
        "usage": result.get("usage") or {},
        "elapsed_ms": result.get("elapsed_ms", 0),
    }
    if reverse_agg:
        out["reverse_image_search"] = {
            "requested": reverse_agg.get("requested", True),
            "serpapi": reverse_agg.get("serpapi", {}),
            "saucenao": reverse_agg.get("saucenao", {}),
            "notes": reverse_agg.get("notes", []),
        }
    _write_output(out, args.output, evidence_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
