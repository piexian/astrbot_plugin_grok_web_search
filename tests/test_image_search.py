"""反向搜图共享逻辑回归：拦截、编排、归一化与图片预处理。"""

import asyncio
import base64

from PIL import Image

from tool.image_search import (
    SERPAPI_MAX_BYTES,
    format_evidence,
    normalize_saucenao_payload,
    normalize_serpapi_payload,
    plan_backends,
    prepare_serpapi_image,
    run_reverse_image_search,
    validate_images,
)


def _png_b64(size=(8, 8), color=(255, 0, 0)) -> str:
    from io import BytesIO

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ─── 图片校验 ───────────────────────────────────────────


def test_validate_images_drops_invalid_and_dedups():
    good = _png_b64()
    valid, invalid = validate_images([good, "not-base64!!", "", good])
    assert valid == [good]
    assert invalid == 2


def test_validate_images_empty():
    assert validate_images([]) == ([], 0)
    assert validate_images(None) == ([], 0)  # type: ignore[arg-type]


# ─── 本地规划与拦截 ──────────────────────────────────────


def test_plan_no_images_blocks_backends():
    plan = plan_backends(
        use_serpapi=True,
        use_saucenao=True,
        valid_images=[],
        serpapi_key="k",
        saucenao_key="k",
    )
    assert plan["requested"]
    assert plan["images"] == []
    assert not any(plan["backends"].values())
    assert any("未产生搜图请求" in n for n in plan["notes"])


def test_plan_not_requested_is_noop():
    plan = plan_backends(
        use_serpapi=False,
        use_saucenao=False,
        valid_images=["abc"],
        serpapi_key="k",
        saucenao_key="k",
    )
    assert not plan["requested"]
    assert plan["notes"] == []


def test_plan_missing_key_skips_backend():
    plan = plan_backends(
        use_serpapi=True,
        use_saucenao=True,
        valid_images=["abc"],
        serpapi_key="",
        saucenao_key="k",
    )
    assert plan["backends"] == {"serpapi": False, "saucenao": True}
    assert any("SerpAPI Key" in n for n in plan["notes"])


def test_plan_caps_images():
    plan = plan_backends(
        use_serpapi=True,
        use_saucenao=False,
        valid_images=["a", "b", "c", "d"],
        serpapi_key="k",
        saucenao_key="",
        max_images=3,
    )
    assert plan["images"] == ["a", "b", "c"]
    assert any("上限" in n for n in plan["notes"])


# ─── 编排：无图零请求 / 单后端失败不互相影响 ───────────────


def test_run_no_images_zero_backend_calls():
    calls = []

    async def fake_fn(image, *, api_key, timeout, proxy=None):
        calls.append(image)
        return {"ok": True, "payload": {}, "error": ""}

    agg = asyncio.run(
        run_reverse_image_search(
            [],
            use_serpapi=True,
            use_saucenao=True,
            serpapi_key="k",
            saucenao_key="k",
            serpapi_fn=fake_fn,
            saucenao_fn=fake_fn,
        )
    )
    assert calls == []
    assert agg["serpapi"]["matches"] == []
    assert "未检测到可用图片" in agg["evidence_text"]


def test_run_not_requested_skips_everything():
    calls = []

    async def fake_fn(image, *, api_key, timeout, proxy=None):
        calls.append(image)
        return {"ok": True, "payload": {}, "error": ""}

    agg = asyncio.run(
        run_reverse_image_search(
            [_png_b64()],
            use_serpapi=False,
            use_saucenao=False,
            serpapi_fn=fake_fn,
            saucenao_fn=fake_fn,
        )
    )
    assert calls == []
    assert agg["evidence_text"] == ""


def test_run_one_backend_failure_keeps_other():
    serpapi_payload = {
        "visual_matches": [{"title": "T", "link": "https://a.example/x", "source": "s"}]
    }

    async def serpapi_ok(image, *, api_key, timeout, proxy=None):
        return {"ok": True, "payload": serpapi_payload, "error": ""}

    async def saucenao_fail(image, *, api_key, timeout, proxy=None):
        return {"ok": False, "payload": None, "error": "额度用尽"}

    agg = asyncio.run(
        run_reverse_image_search(
            [_png_b64()],
            use_serpapi=True,
            use_saucenao=True,
            serpapi_key="k",
            saucenao_key="k",
            serpapi_fn=serpapi_ok,
            saucenao_fn=saucenao_fail,
        )
    )
    assert len(agg["serpapi"]["matches"]) == 1
    assert agg["saucenao"]["matches"] == []
    assert any("额度用尽" in n for n in agg["notes"])
    assert "SerpAPI candidates" in agg["evidence_text"]


def test_run_caps_images_via_plan():
    calls = []

    async def fake_fn(image, *, api_key, timeout, proxy=None):
        calls.append(image)
        return {"ok": True, "payload": {}, "error": ""}

    asyncio.run(
        run_reverse_image_search(
            [_png_b64(color=c) for c in ((1, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 0))],
            use_serpapi=True,
            use_saucenao=False,
            serpapi_key="k",
            saucenao_key="",
            max_images=2,
            serpapi_fn=fake_fn,
        )
    )
    assert len(calls) == 2


def test_run_evidence_has_no_display_metadata():
    async def ok_fn(image, *, api_key, timeout, proxy=None):
        return {"ok": True, "payload": {}, "error": ""}

    agg = asyncio.run(
        run_reverse_image_search(
            [_png_b64()],
            use_serpapi=True,
            use_saucenao=True,
            serpapi_key="k",
            saucenao_key="k",
            serpapi_fn=ok_fn,
            saucenao_fn=ok_fn,
        )
    )
    assert "耗时" not in agg["evidence_text"]
    assert "tokens" not in agg["evidence_text"].lower()


# ─── 归一化 ─────────────────────────────────────────────


def test_normalize_serpapi_payload():
    payload = {
        "knowledge_graph": {"title": "角色X"},
        "visual_matches": [
            {"title": "A", "link": "https://e/1", "source": "pixiv"},
            {"title": "B", "link": "https://e/1", "source": "danbooru"},
        ],
    }
    matches = normalize_serpapi_payload(payload)
    assert matches[0] == {"title": "角色X", "url": "", "source": "Google Lens"}
    assert [m["title"] for m in matches[1:]] == ["A"]  # 重复 URL 去重


def test_normalize_serpapi_payload_non_dict():
    assert normalize_serpapi_payload(None) == []
    assert normalize_serpapi_payload("x") == []


def test_normalize_saucenao_payload():
    payload = {
        "header": {"status": 0, "user_id": 1},
        "results": [
            {
                "header": {"index_name": "Index #5: Pixiv", "similarity": "93.21"},
                "data": {
                    "title": "めぐみん",
                    "member_name": "author1",
                    "ext_urls": ["https://www.pixiv.net/artworks/1"],
                },
            }
        ],
    }
    assert normalize_saucenao_payload(payload) == [
        {
            "title": "めぐみん",
            "url": "https://www.pixiv.net/artworks/1",
            "source": "Index #5: Pixiv",
            "author": "author1",
            "similarity": "93.2%",
        }
    ]


def test_normalize_saucenao_payload_non_dict():
    assert normalize_saucenao_payload(None) == []
    assert normalize_saucenao_payload({"results": "x"}) == []


# ─── SerpAPI 图片预处理 ──────────────────────────────────


def test_prepare_serpapi_small_png_passthrough():
    prepared = prepare_serpapi_image(_png_b64())
    assert prepared is not None
    data, mime = prepared
    assert mime == "image/png"
    assert len(data) <= SERPAPI_MAX_BYTES


def test_prepare_serpapi_gif_converted():
    from io import BytesIO

    buf = BytesIO()
    Image.new("P", (8, 8), 0).save(buf, format="GIF")
    gif_b64 = base64.b64encode(buf.getvalue()).decode()
    prepared = prepare_serpapi_image(gif_b64)
    assert prepared is not None
    _data, mime = prepared
    assert mime == "image/jpeg"


def test_prepare_serpapi_compresses_oversize():
    buf_image = Image.effect_noise((2400, 2400), 64)
    from io import BytesIO

    buf = BytesIO()
    buf_image.save(buf, format="PNG")
    assert buf.tell() > SERPAPI_MAX_BYTES  # 前提：原图超限
    prepared = prepare_serpapi_image(base64.b64encode(buf.getvalue()).decode())
    assert prepared is not None
    data, mime = prepared
    assert mime == "image/jpeg"
    assert len(data) <= SERPAPI_MAX_BYTES


def test_prepare_serpapi_rejects_invalid():
    assert prepare_serpapi_image("not-base64!!") is None


# ─── 证据块格式 ──────────────────────────────────────────


def test_format_evidence_skips_empty():
    assert format_evidence({"serpapi": {}, "saucenao": {}, "notes": []}) == ""


def test_format_evidence_renders_matches_and_notes():
    agg = {
        "serpapi": {
            "matches": [{"title": "A", "url": "https://e/1", "source": "pixiv"}]
        },
        "saucenao": {"matches": []},
        "notes": ["未配置 SauceNAO Key，已跳过 SauceNAO 搜图"],
    }
    text = format_evidence(agg)
    assert "[Reverse image search evidence]" in text
    assert "A" in text and "https://e/1" in text
    assert "非确认结论" in text


def test_run_passes_proxy_to_backends():
    seen = {}

    async def fake_fn(image, *, api_key, timeout, proxy=None):
        seen["proxy"] = proxy
        return {"ok": True, "payload": {}, "error": ""}

    asyncio.run(
        run_reverse_image_search(
            [_png_b64()],
            use_serpapi=True,
            use_saucenao=False,
            serpapi_key="k",
            saucenao_key="",
            serpapi_fn=fake_fn,
            proxy="http://127.0.0.1:7890",
        )
    )
    assert seen["proxy"] == "http://127.0.0.1:7890"


def test_run_passes_none_proxy_when_unset():
    seen = {}

    async def fake_fn(image, *, api_key, timeout, proxy=None):
        seen["proxy"] = proxy
        return {"ok": True, "payload": {}, "error": ""}

    asyncio.run(
        run_reverse_image_search(
            [_png_b64()],
            use_serpapi=True,
            use_saucenao=False,
            serpapi_key="k",
            saucenao_key="",
            serpapi_fn=fake_fn,
            proxy="",
        )
    )
    assert seen["proxy"] is None
