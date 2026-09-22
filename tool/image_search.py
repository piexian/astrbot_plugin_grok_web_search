"""反向搜图共享逻辑：/grok 参数解析、图片校验、后端编排与结果归一化。

供 main.py（LLM Tool / /grok）与 skill/scripts/grok_search.py 共用，
保证两个入口的拦截语义与结果契约一致。
"""

import asyncio
import base64
from collections.abc import Callable, Coroutine
from typing import Any

from .tool import normalize_image

# ─── 常量 ───────────────────────────────────────────────

# /grok 指令支持的搜索深度
VALID_CMD_DEPTHS = ("basic", "advanced", "deep")
# SerpAPI Image API 限制：格式与大小（https://serpapi.com/image-api）
SERPAPI_MIMES = {"image/jpeg", "image/png", "image/webp"}
SERPAPI_MAX_BYTES = 500 * 1000
# 默认反向搜图配置（与 main.py CONFIG_DEFAULTS / skill _CONFIG_DEFAULTS 保持一致）
DEFAULT_IMAGE_SEARCH_TIMEOUT = 30
DEFAULT_IMAGE_SEARCH_MAX_IMAGES = 3
# 每家后端最多采纳的候选条数
_MAX_MATCHES_PER_BACKEND = 5

_CMD_FLAGS_BOOL = {"--serpapi", "--saucenao", "--all"}
_CMD_FLAGS_DEPTH = {"--search-depth", "--depth"}
_BACKEND_LABELS = {"serpapi": "SerpAPI", "saucenao": "SauceNAO"}

# 后端适配函数签名：(image: tuple[bytes, str], *, api_key, timeout) -> dict
BackendFn = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


def parse_cmd_args(text: str) -> dict[str, Any]:
    """解析 /grok 正文开头的参数。

    支持 --serpapi / --saucenao / --all / --search-depth X / --depth X /
    --（终止符，其后按原文搜索）。未知参数与非法深度返回 ok=False。
    """
    result: dict[str, Any] = {
        "ok": False,
        "error": "",
        "use_serpapi": False,
        "use_saucenao": False,
        "all": False,
        "search_depth": "basic",
        "query": "",
    }
    s = text
    n = len(s)
    i = 0
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n or not s.startswith("--", i):
            break
        j = i
        while j < n and not s[j].isspace():
            j += 1
        tok = s[i:j]
        if tok == "--":
            i = j  # 终止符：其余全部按原文
            break
        name, sep, inline = tok.partition("=")
        if name in _CMD_FLAGS_BOOL:
            if sep:
                result["error"] = f"参数 {name} 不接受取值"
                return result
            if name == "--serpapi":
                result["use_serpapi"] = True
            elif name == "--saucenao":
                result["use_saucenao"] = True
            else:
                result["all"] = True
            i = j
            continue
        if name in _CMD_FLAGS_DEPTH:
            value = inline
            if not sep:
                k = j
                while k < n and s[k].isspace():
                    k += 1
                m = k
                while m < n and not s[m].isspace():
                    m += 1
                if k >= n:
                    result["error"] = f"参数 {name} 缺少取值（basic/advanced/deep）"
                    return result
                value = s[k:m]
                j = m
            value = value.strip().lower()
            if value not in VALID_CMD_DEPTHS:
                result["error"] = (
                    f"无效的搜索深度: {value or '(空)'}，可选 basic/advanced/deep"
                )
                return result
            result["search_depth"] = value
            i = j
            continue
        result["error"] = (
            f"未知参数 {name}，支持 --serpapi --saucenao --all --depth 与 --"
        )
        return result
    # --all 固定全开 + deep，不受参数顺序影响
    if result["all"]:
        result["use_serpapi"] = True
        result["use_saucenao"] = True
        result["search_depth"] = "deep"
    result["query"] = s[i:].strip()
    result["ok"] = True
    return result


def validate_images(images: list[str]) -> tuple[list[str], int]:
    """校验 base64 图片列表，返回（去重、格式归一化后的有效图片, 无效数量）。"""
    valid: list[str] = []
    seen: set[str] = set()
    invalid = 0
    for b64 in images or []:
        if not b64 or not isinstance(b64, str):
            invalid += 1
            continue
        normalized = normalize_image(b64)
        if normalized is None:
            invalid += 1
            continue
        _mime, data = normalized
        if data in seen:
            continue
        seen.add(data)
        valid.append(data)
    return valid, invalid


def prepare_serpapi_image(b64: str) -> tuple[bytes, str] | None:
    """把 base64 图片准备为 SerpAPI Image API 可接受的 (bytes, mime)。

    SerpAPI 仅接受 JPEG/PNG/WebP 且不超过 500KB：GIF 等格式转码，超限做有界
    压缩（最多缩放 3 次）；仍超限返回 None，由调用方跳过以避免无意义请求。
    """
    normalized = normalize_image(b64)
    if normalized is None:
        return None
    mime, data = normalized
    try:
        raw = base64.b64decode(data)
    except Exception:
        return None
    if mime in SERPAPI_MIMES and len(raw) <= SERPAPI_MAX_BYTES:
        return raw, mime
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return None
    try:
        bio = BytesIO(raw)
        img = Image.open(bio)
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)  # GIF 仅取首帧
        img = img.convert("RGB")
        for _attempt in range(4):
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=80)
            data_bytes = buf.getvalue()
            if len(data_bytes) <= SERPAPI_MAX_BYTES:
                return data_bytes, "image/jpeg"
            img = img.resize(
                (max(1, int(img.width * 0.75)), max(1, int(img.height * 0.75)))
            )
        return None
    except Exception:
        return None
    finally:
        bio.close()


def plan_backends(
    *,
    use_serpapi: bool,
    use_saucenao: bool,
    valid_images: list[str],
    serpapi_key: str,
    saucenao_key: str,
    max_images: int = DEFAULT_IMAGE_SEARCH_MAX_IMAGES,
) -> dict[str, Any]:
    """本地规划反向搜图：无图拦截、Key 检查与数量上限，不产生任何网络请求。"""
    notes: list[str] = []
    backends: dict[str, bool] = {"serpapi": False, "saucenao": False}
    if not (use_serpapi or use_saucenao):
        return {"images": [], "backends": backends, "notes": notes, "requested": False}
    if not valid_images:
        notes.append("未检测到可用图片，已本地跳过反向搜图（未产生搜图请求）")
        return {"images": [], "backends": backends, "notes": notes, "requested": True}
    if use_serpapi and not serpapi_key:
        notes.append("未配置 SerpAPI Key，已跳过 Google Lens 搜图")
    else:
        backends["serpapi"] = use_serpapi
    if use_saucenao and not saucenao_key:
        notes.append("未配置 SauceNAO Key，已跳过 SauceNAO 搜图")
    else:
        backends["saucenao"] = use_saucenao
    images = valid_images
    if max_images > 0 and len(images) > max_images:
        notes.append(
            f"图片数量超过上限 {max_images}，仅对前 {max_images} 张执行反向搜图"
        )
        images = images[:max_images]
    return {"images": images, "backends": backends, "notes": notes, "requested": True}


def normalize_serpapi_payload(payload: Any) -> list[dict[str, str]]:
    """把 SerpAPI Google Lens JSON 归一化为候选列表（按 URL 去重，截取前 N 条）。"""
    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(payload, dict):
        return matches
    kg = payload.get("knowledge_graph")
    if isinstance(kg, dict) and str(kg.get("title") or "").strip():
        matches.append(
            {"title": str(kg["title"]).strip(), "url": "", "source": "Google Lens"}
        )
    visual = payload.get("visual_matches")
    if isinstance(visual, list):
        for item in visual:
            if len(matches) >= _MAX_MATCHES_PER_BACKEND:
                break
            if not isinstance(item, dict):
                continue
            url = str(item.get("link") or "").strip()
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            matches.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": url,
                    "source": str(item.get("source") or "").strip(),
                }
            )
    return matches


def normalize_saucenao_payload(payload: Any) -> list[dict[str, str]]:
    """把 SauceNAO JSON 归一化为候选列表（相似度仅表示图像匹配程度）。"""
    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return matches
    for item in payload["results"]:
        if len(matches) >= _MAX_MATCHES_PER_BACKEND:
            break
        if not isinstance(item, dict):
            continue
        header = item.get("header")
        data = item.get("data")
        if not isinstance(header, dict) or not isinstance(data, dict):
            continue
        ext_urls = data.get("ext_urls")
        url = ""
        if isinstance(ext_urls, list) and ext_urls:
            url = str(ext_urls[0] or "").strip()
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        similarity = ""
        try:
            similarity = f"{float(header.get('similarity')):.1f}%"
        except (TypeError, ValueError):
            pass
        matches.append(
            {
                "title": str(data.get("title") or data.get("material") or "").strip(),
                "url": url,
                "source": str(header.get("index_name") or "").strip(),
                "author": str(
                    data.get("member_name") or data.get("author") or ""
                ).strip(),
                "similarity": similarity,
            }
        )
    return matches


async def run_reverse_image_search(
    images: list[str],
    *,
    use_serpapi: bool,
    use_saucenao: bool,
    serpapi_key: str = "",
    saucenao_key: str = "",
    timeout: float = DEFAULT_IMAGE_SEARCH_TIMEOUT,
    max_images: int = DEFAULT_IMAGE_SEARCH_MAX_IMAGES,
    serpapi_fn: BackendFn | None = None,
    saucenao_fn: BackendFn | None = None,
    proxy: str = "",
) -> dict[str, Any]:
    """反向搜图编排：校验 → 本地规划 → 并发调用注入的后端 → 归一化。

    serpapi_fn / saucenao_fn 见 api 包适配函数；注入便于测试与 Skill 复用。
    无有效图片时本地拦截，不会调用任何后端；单后端失败不影响另一家结果。
    proxy 沿用 connection_settings.proxy，透传给后端适配函数。
    """
    valid, invalid_count = validate_images(images)
    plan = plan_backends(
        use_serpapi=use_serpapi,
        use_saucenao=use_saucenao,
        valid_images=valid,
        serpapi_key=serpapi_key,
        saucenao_key=saucenao_key,
        max_images=max_images,
    )
    notes = list(plan["notes"])
    if invalid_count:
        notes.append(f"{invalid_count} 张图片无法识别，已忽略")
    agg: dict[str, Any] = {
        "requested": plan["requested"],
        "serpapi": {"ok": False, "matches": [], "error": ""},
        "saucenao": {"ok": False, "matches": [], "error": ""},
        "notes": notes,
    }
    tasks: list[Coroutine[Any, Any, dict[str, Any]]] = []
    if plan["backends"]["serpapi"] and serpapi_fn is not None:
        tasks.append(
            _run_backend(
                serpapi_fn,
                plan["images"],
                serpapi_key,
                timeout,
                "serpapi",
                prepare_serpapi_image,
                proxy,
            )
        )
    if plan["backends"]["saucenao"] and saucenao_fn is not None:
        tasks.append(
            _run_backend(
                saucenao_fn,
                plan["images"],
                saucenao_key,
                timeout,
                "saucenao",
                None,
                proxy,
            )
        )
    if tasks:
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, BaseException):
                notes.append(f"反向搜图后端异常: {type(res).__name__}")
                continue
            agg[res["backend"]] = {
                "ok": res["ok"],
                "matches": res["matches"],
                "error": res["error"],
            }
            notes.extend(res["notes"])
    agg["notes"] = notes
    agg["evidence_text"] = format_evidence(agg)
    return agg


async def _run_backend(
    fn: BackendFn,
    images_b64: list[str],
    api_key: str,
    timeout: float,
    backend: str,
    prepare: Callable[[str], tuple[bytes, str] | None] | None = None,
    proxy: str = "",
) -> dict[str, Any]:
    """对单家后端逐张执行搜图（顺序执行，单张失败不中断本后端）。"""
    label = _BACKEND_LABELS[backend]
    matches: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    notes: list[str] = []
    error = ""
    for idx, b64 in enumerate(images_b64, start=1):
        if prepare is not None:
            image = prepare(b64)
            if image is None:
                notes.append(f"{label} 第 {idx} 张图片格式不支持或压缩后仍超限，已跳过")
                continue
        else:
            try:
                image = (base64.b64decode(b64), "application/octet-stream")
            except Exception:
                notes.append(f"{label} 第 {idx} 张图片解码失败，已跳过")
                continue
        try:
            res = await fn(image, api_key=api_key, timeout=timeout, proxy=proxy or None)
        except Exception as e:  # 适配层不应抛出，此处兜底
            res = {"ok": False, "payload": None, "error": str(e)}
        if not res.get("ok"):
            error = str(res.get("error") or "未知错误")
            notes.append(f"{label} 第 {idx} 张图片搜索失败: {error}")
            continue
        payload = res.get("payload")
        normalized = (
            normalize_serpapi_payload(payload)
            if backend == "serpapi"
            else normalize_saucenao_payload(payload)
        )
        for m in normalized:
            url = m.get("url") or ""
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            matches.append(m)
    return {
        "backend": backend,
        "ok": bool(matches),
        "matches": matches,
        "error": error,
        "notes": notes,
    }


def format_evidence(agg: dict[str, Any]) -> str:
    """把反向搜图聚合结果格式化为附加给主模型的证据块。

    仅包含候选来源与本地说明，不含耗时/额度等展示信息；全空时返回空字符串。
    """
    lines: list[str] = []
    for backend in ("serpapi", "saucenao"):
        entry = agg.get(backend) or {}
        if entry.get("matches"):
            if lines:
                lines.append("")
            lines.append(f"{_BACKEND_LABELS[backend]} candidates:")
            lines.extend(_format_match_lines(entry["matches"]))
    notes = list(agg.get("notes") or [])
    if notes:
        if lines:
            lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in notes)
    if not lines:
        return ""
    lines.append(
        "以上为反向搜图候选来源，非确认结论；请优先依据这些线索核验图片内容，"
        "不要沿用未经核验的角色名。"
    )
    return "[Reverse image search evidence]\n" + "\n".join(lines)


def _format_match_lines(matches: list[dict[str, str]]) -> list[str]:
    lines = []
    for idx, m in enumerate(matches, start=1):
        parts = [f"{idx}. {m.get('title') or '(无标题)'}"]
        meta = [p for p in (m.get("source"), m.get("author"), m.get("similarity")) if p]
        if meta:
            parts.append(f"({'; '.join(meta)})")
        if m.get("url"):
            parts.append(m["url"])
        lines.append(" ".join(parts))
    return lines
