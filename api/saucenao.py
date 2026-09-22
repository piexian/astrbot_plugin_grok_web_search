"""SauceNAO 反向搜图适配：multipart 字段 file 直接上传图片检索。"""

import asyncio
from typing import Any

import aiohttp

SAUCENAO_URL = "https://saucenao.com/search.php"
DEFAULT_TIMEOUT = 30.0
_MAX_NUMRES = 5


async def saucenao_search(
    image: tuple[bytes, str],
    *,
    api_key: str,
    timeout: float = DEFAULT_TIMEOUT,
    proxy: str | None = None,
    numres: int = _MAX_NUMRES,
) -> dict[str, Any]:
    """上传单张图片执行 SauceNAO 检索。

    Returns:
        {"ok": bool, "payload": dict | None, "error": str}
    """
    data, mime = image
    params = {
        "api_key": api_key,
        "output_type": 2,  # JSON
        "numres": max(1, min(int(numres), 40)),
        "testmode": 0,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=float(timeout))
        ) as session:
            form = aiohttp.FormData()
            form.add_field(
                "file",
                data,
                filename="image",
                content_type=mime or "application/octet-stream",
            )
            async with session.post(
                SAUCENAO_URL, params=params, data=form, proxy=proxy
            ) as resp:
                if resp.status == 403:
                    return {
                        "ok": False,
                        "payload": None,
                        "error": "SauceNAO API Key 无效",
                    }
                if resp.status == 413:
                    return {"ok": False, "payload": None, "error": "SauceNAO 图片过大"}
                if resp.status == 429:
                    return {
                        "ok": False,
                        "payload": None,
                        "error": "SauceNAO 搜索额度已达上限（短期/长期）",
                    }
                if resp.status != 200:
                    return {
                        "ok": False,
                        "payload": None,
                        "error": f"SauceNAO 搜索失败: HTTP {resp.status}",
                    }
                payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "payload": None,
                "error": "SauceNAO 返回了非 JSON 内容",
            }
        header = payload.get("header")
        if isinstance(header, dict):
            err = _verify_header(header)
            if err:
                return {"ok": False, "payload": None, "error": err}
        return {"ok": True, "payload": payload, "error": ""}
    except asyncio.TimeoutError:
        return {"ok": False, "payload": None, "error": "SauceNAO 请求超时"}
    except aiohttp.ClientError as e:
        return {
            "ok": False,
            "payload": None,
            "error": f"SauceNAO 请求失败: {type(e).__name__}",
        }
    except Exception as e:  # 兜底：含 JSON 解析失败
        return {
            "ok": False,
            "payload": None,
            "error": f"SauceNAO 响应解析失败: {type(e).__name__}",
        }


def _verify_header(header: dict[str, Any]) -> str:
    """按官方示例校验响应头，返回错误说明（无错误返回空串）。"""
    try:
        status = int(header.get("status"))
    except (TypeError, ValueError):
        status = 0
    if status < 0:
        return f"SauceNAO 请求错误（status={status}）"
    if status > 0:
        return f"SauceNAO 服务错误（status={status}）"
    try:
        user_id = int(header.get("user_id"))
    except (TypeError, ValueError):
        user_id = 0
    if user_id == 0:
        return "SauceNAO API Key 无效"
    short_remaining = header.get("short_remaining")
    if isinstance(short_remaining, (int, float)) and short_remaining < 0:
        return "SauceNAO 30 秒额度已用尽"
    long_remaining = header.get("long_remaining")
    if isinstance(long_remaining, (int, float)) and long_remaining < 0:
        return "SauceNAO 24 小时额度已用尽"
    return ""
