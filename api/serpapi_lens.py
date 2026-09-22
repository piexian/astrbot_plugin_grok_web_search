"""SerpAPI Google Lens 反向搜图适配。

流程：POST /image 上传图片获取 image_id → GET /search?engine=google_lens。
首轮检索不携带文字 q，避免把主模型猜测的角色名当作检索条件。
"""

import asyncio
from typing import Any

import aiohttp

SERPAPI_BASE_URL = "https://serpapi.com"
DEFAULT_TIMEOUT = 30.0


async def serpapi_lens_search(
    image: tuple[bytes, str],
    *,
    api_key: str,
    timeout: float = DEFAULT_TIMEOUT,
    proxy: str | None = None,
) -> dict[str, Any]:
    """上传单张图片并执行 Google Lens 搜索。

    Args:
        image: (图片二进制, mime)；需已满足 SerpAPI 的格式与 500KB 限制
        api_key: SerpAPI API Key
        timeout: 请求超时（秒）
        proxy: 可选 HTTP 代理

    Returns:
        {"ok": bool, "payload": dict | None, "error": str}
    """
    data, mime = image
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=float(timeout))
        ) as session:
            form = aiohttp.FormData()
            form.add_field("image", data, content_type=mime, filename="image")
            form.add_field("api_key", api_key)
            async with session.post(
                f"{SERPAPI_BASE_URL}/image", data=form, proxy=proxy
            ) as resp:
                body = await resp.json(content_type=None)
                image_id = ""
                if isinstance(body, dict):
                    image_id = str(body.get("image_id") or "")
                if resp.status != 200 or not image_id:
                    detail = ""
                    if isinstance(body, dict):
                        detail = str(body.get("error") or "")
                    return {
                        "ok": False,
                        "payload": None,
                        "error": f"图片上传失败: {detail or f'HTTP {resp.status}'}",
                    }
            params = {
                "engine": "google_lens",
                "image_id": image_id,
                "api_key": api_key,
            }
            async with session.get(
                f"{SERPAPI_BASE_URL}/search", params=params, proxy=proxy
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    return {
                        "ok": False,
                        "payload": None,
                        "error": f"Google Lens 搜索失败: HTTP {resp.status}",
                    }
                if isinstance(payload, dict) and payload.get("error"):
                    return {
                        "ok": False,
                        "payload": None,
                        "error": f"Google Lens 搜索失败: {payload['error']}",
                    }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "payload": None,
                "error": "Google Lens 返回了非 JSON 内容",
            }
        return {"ok": True, "payload": payload, "error": ""}
    except asyncio.TimeoutError:
        return {"ok": False, "payload": None, "error": "SerpAPI 请求超时"}
    except aiohttp.ClientError as e:
        return {
            "ok": False,
            "payload": None,
            "error": f"SerpAPI 请求失败: {type(e).__name__}",
        }
    except Exception as e:  # 兜底：含 JSON 解析失败
        return {
            "ok": False,
            "payload": None,
            "error": f"SerpAPI 响应解析失败: {type(e).__name__}",
        }
