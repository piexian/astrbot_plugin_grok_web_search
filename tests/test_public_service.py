"""SDK v1 公开服务契约：真实门面 + 真实 Main 生命周期 + 真实共享编排。

HTTP/网络在 aiohttp 边界用可计数替身拦截；连通性检查、字体安装与
Skill 管理副作用同样被拦截，测试不触网、不访问真实登录态或数据目录。
需要完整 astrbot 运行时（与生产一致）；轻量 CI 环境无宿主依赖时跳过。
"""

import asyncio
import base64
import json

import aiohttp
import astrbot.api
import pytest
from conftest import load

if getattr(astrbot.api, "__spec__", None) is None:
    pytest.skip("SDK Main 契约测试需要完整 astrbot 运行时", allow_module_level=True)

main_mod = load("main")

public_api = load("public_api")
fetch_service = load("tool.fetch_service")
font_loader = load("tool.font_loader")
chat = load("api.grok_chat")

PAGE = "# SDK fixture page\n\nReal content from the fake upstream."


# ─── 网络替身 ────────────────────────────────────────────────


class _Response:
    def __init__(self, payload, status=200, content_type="application/json"):
        self.payload = payload
        self.status = status
        self.headers = {"Content-Type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.payload


class _HTTPStub:
    """aiohttp.ClientSession 替身：按 URL 后缀路由响应，记录全部请求。"""

    def __init__(self, monkeypatch):
        self.calls: list[dict] = []
        self.routes: dict[str, object] = {}
        stub = self

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def _send(self, method, url, **kwargs):
                record = {"method": method, "url": url}
                if method == "POST":
                    record["json"] = kwargs.get("json")
                stub.calls.append(record)
                for suffix, handler in stub.routes.items():
                    if url.endswith(suffix):
                        if isinstance(handler, Exception):
                            raise handler
                        return handler
                raise AssertionError(f"测试未注册该网络请求: {method} {url}")

            def get(self, url, **kwargs):
                return self._send("GET", url, **kwargs)

            def post(self, url, **kwargs):
                return self._send("POST", url, **kwargs)

        monkeypatch.setattr(aiohttp, "ClientSession", _Session)

    def route(self, suffix, handler):
        self.routes[suffix] = handler

    def gets(self):
        return [c for c in self.calls if c["method"] == "GET"]

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


def _chat_reply(content="Answer", sources=None, model="grok-fixture", usage=None):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"content": content, "sources": sources or []}
                    )
                }
            }
        ],
        "model": model,
        "usage": usage or {"total_tokens": 42},
    }
    return _Response(json.dumps(payload))


def _fetch_reply(content=PAGE, model="srv-model"):
    """抓取响应：正文原样返回，不走搜索 JSON 协议（FETCH_SYSTEM_PROMPT）。"""
    payload = {
        "choices": [{"message": {"content": content}}],
        "model": model,
        "usage": {"total_tokens": 7},
    }
    return _Response(json.dumps(payload))


# ─── 真实 Main 装配（拦截 Skill/网络副作用） ──────────────────


def _base_config(**overrides):
    config = {
        "connection_settings": {
            "base_url": "https://grok.example",
            "api_key": "sdk-fixture-key",
        },
        "provider_settings": {"model": "grok-global"},
        "tool_settings": {"enable_fetch": True},
        "reverse_image_search": {
            "serpapi_api_key": "serp-fixture-key",
            "saucenao_api_key": "",
        },
    }
    config.update(overrides)
    return config


def _make_plugin(monkeypatch, config=None):
    stub = _HTTPStub(monkeypatch)
    stub.route("/v1/models", _Response("{}", 200))
    monkeypatch.setattr(
        main_mod.GrokSearchPlugin, "_get_skill_manager", lambda self: None
    )
    plugin = main_mod.GrokSearchPlugin(context=None, config=config or _base_config())
    return plugin, stub


def _initialized_plugin(monkeypatch, config=None):
    plugin, stub = _make_plugin(monkeypatch, config)
    asyncio.run(plugin.initialize())
    return plugin, stub


def _png_b64() -> str:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (2, 2), (200, 30, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ─── 版本协商与实例语义 ───────────────────────────────────────


def test_get_service_identity_and_version_negotiation(monkeypatch):
    plugin, _ = _make_plugin(monkeypatch)
    svc = plugin.get_service()
    assert plugin.get_service() is svc
    assert plugin.get_service(1) is svc
    assert plugin.get_service(api_version=1) is svc
    assert type(svc.instance_id) is str and svc.instance_id
    for bad in (2, 0, -1, "1", 1.0, True):
        with pytest.raises(public_api.PluginServiceError) as ei:
            plugin.get_service(bad)
        assert ei.value.code == "unsupported_version"


def test_new_plugin_instance_gets_new_service_instance(monkeypatch):
    plugin_a, _ = _make_plugin(monkeypatch)
    plugin_b, _ = _make_plugin(monkeypatch)
    svc_a = plugin_a.get_service()
    svc_b = plugin_b.get_service()
    assert svc_a is not svc_b
    assert svc_a.instance_id != svc_b.instance_id


def test_capabilities_are_static_feature_list(monkeypatch):
    plugin, _ = _make_plugin(monkeypatch)
    caps = plugin.get_service().capabilities()
    assert caps == {
        "api_version": 1,
        "features": ["web.search", "web.fetch", "image.search"],
    }


# ─── 状态：本地解析、无副作用、不泄露配置 ─────────────────────


def test_status_before_initialize_is_initializing(monkeypatch):
    plugin, _ = _make_plugin(monkeypatch)
    status = plugin.get_service().get_status()
    assert status["state"] == "initializing"
    assert status["ready"] is False
    assert status["api_version"] == 1
    assert status["reason"] is None


def test_status_after_initialize_is_ready(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)
    status = plugin.get_service().get_status()
    assert status["state"] == "ready"
    assert status["ready"] is True
    assert status["reason"] is None
    assert status["search_ready"] is True
    assert status["fetch_ready"] is True
    assert status["image_search_ready"] is True  # 仅配置了 SerpAPI Key


def test_status_ready_with_only_image_search_credentials(monkeypatch):
    config = _base_config(
        connection_settings={"base_url": "", "api_key": ""},
        reverse_image_search={"serpapi_api_key": "serp-only", "saucenao_api_key": ""},
    )
    plugin, _ = _initialized_plugin(monkeypatch, config)
    status = plugin.get_service().get_status()
    assert status["state"] == "ready"
    assert status["image_search_ready"] is True
    assert status["search_ready"] is False
    assert status["fetch_ready"] is False


def test_status_fetch_flag_follows_enable_fetch(monkeypatch):
    config = _base_config(tool_settings={"enable_fetch": False})
    plugin, _ = _initialized_plugin(monkeypatch, config)
    status = plugin.get_service().get_status()
    assert status["search_ready"] is True
    assert status["fetch_ready"] is False
    assert status["state"] == "ready"


def test_status_unavailable_without_any_configuration(monkeypatch):
    config = _base_config(
        connection_settings={"base_url": "", "api_key": ""},
        reverse_image_search={"serpapi_api_key": "", "saucenao_api_key": ""},
    )
    plugin, _ = _initialized_plugin(monkeypatch, config)
    status = plugin.get_service().get_status()
    assert status["state"] == "unavailable"
    assert status["ready"] is False
    assert status["reason"] == "not_configured"
    assert not any(
        status[k] for k in ("search_ready", "fetch_ready", "image_search_ready")
    )


def test_status_ignores_placeholder_credentials(monkeypatch):
    config = _base_config(
        connection_settings={"base_url": "YOUR_BASE_URL", "api_key": "CHANGE_ME"},
        reverse_image_search={"serpapi_api_key": "", "saucenao_api_key": ""},
    )
    plugin, _ = _initialized_plugin(monkeypatch, config)
    status = plugin.get_service().get_status()
    assert status["search_ready"] is False
    assert status["state"] == "unavailable"


def test_status_and_capabilities_have_no_side_effects(monkeypatch):
    config = _base_config(output_settings={"render_as_image": True})
    plugin, _ = _make_plugin(monkeypatch, config)

    def _boom(*args, **kwargs):
        raise AssertionError("状态查询不得触发网络/校验/字体/Skill 副作用")

    monkeypatch.setattr(aiohttp, "ClientSession", _boom)
    monkeypatch.setattr(plugin, "_validate_config", _boom)
    monkeypatch.setattr(main_mod.GrokSearchPlugin, "_get_skill_manager", _boom)
    monkeypatch.setattr(font_loader, "begin_job", _boom)

    svc = plugin.get_service()
    for _ in range(2):
        status = svc.get_status()
        assert status["state"] == "initializing"
        assert svc.capabilities()["api_version"] == 1


def test_status_does_not_leak_secrets_or_internal_objects(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)
    svc = plugin.get_service()
    dump = json.dumps({"status": svc.get_status(), "caps": svc.capabilities()})
    assert "sdk-fixture-key" not in dump
    assert "serp-fixture-key" not in dump
    assert "grok.example" not in dump
    assert "extra_headers" not in svc.capabilities()
    assert not hasattr(svc, "config")
    assert not hasattr(svc, "plugin")


def test_config_parse_errors_reported_not_silent(monkeypatch):
    config = _base_config(advanced_settings={"extra_body": "{bad json"})
    plugin, _ = _initialized_plugin(monkeypatch, config)
    svc = plugin.get_service()
    status = svc.get_status()
    assert status["config_errors"] == ["invalid_extra_body"]
    # 状态仍报告能力可用，但业务调用返回明确失败而非空成功
    result = asyncio.run(svc.search("q"))
    assert result["ok"] is False
    assert "extra_body" in result["error"]


# ─── wait_ready ──────────────────────────────────────────────


def test_wait_ready_returns_status_snapshot(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)
    svc = plugin.get_service()
    snapshot = asyncio.run(svc.wait_ready(2))
    assert snapshot == svc.get_status()
    assert snapshot["ready"] is True


def test_wait_ready_times_out_while_initializing(monkeypatch):
    plugin, _ = _make_plugin(monkeypatch)
    with pytest.raises(TimeoutError):
        asyncio.run(plugin.get_service().wait_ready(0.2))


def test_wait_ready_times_out_when_unavailable(monkeypatch):
    config = _base_config(
        connection_settings={"base_url": "", "api_key": ""},
        reverse_image_search={"serpapi_api_key": "", "saucenao_api_key": ""},
    )
    plugin, _ = _initialized_plugin(monkeypatch, config)
    with pytest.raises(TimeoutError):
        asyncio.run(plugin.get_service().wait_ready(0.2))


def test_wait_ready_raises_service_closed(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)
    svc = plugin.get_service()
    asyncio.run(plugin.terminate())
    with pytest.raises(public_api.PluginServiceError) as ei:
        asyncio.run(svc.wait_ready(1))
    assert ei.value.code == "service_closed"


# ─── search：真实共享编排 + 可计数 HTTP 替身 ───────────────────


def test_search_uses_real_chat_orchestration_and_returns_full_dict(monkeypatch):
    plugin, stub = _initialized_plugin(monkeypatch)
    sources = [{"url": "https://example.org/1", "title": "S1", "snippet": "s"}]
    stub.route("/v1/chat/completions", _chat_reply("Answer", sources))
    svc = plugin.get_service()
    result = asyncio.run(svc.search("Python 3.12 news"))
    assert result["ok"] is True
    assert result["content"] == "Answer"
    assert result["sources"] == sources
    assert result["usage"] == {"total_tokens": 42}
    for key in ("elapsed_ms", "retries", "raw", "model"):
        assert key in result
    posts = stub.posts()
    assert len(posts) == 1
    assert posts[0]["url"] == "https://grok.example/v1/chat/completions"
    # 真实共享编排注入了默认 JSON 系统提示词与时间上下文
    body = posts[0]["json"]
    assert body["model"] == "grok-global"


def test_search_dispatches_to_responses_api_when_configured(monkeypatch):
    config = _base_config(provider_settings={"use_responses_api": True})
    plugin, stub = _initialized_plugin(monkeypatch, config)
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"content": "Resp", "sources": []}),
                    }
                ],
            }
        ]
    }
    stub.route("/v1/responses", _Response(json.dumps(payload)))
    result = asyncio.run(plugin.get_service().search("q"))
    assert result["ok"] is True and result["content"] == "Resp"
    assert [c["url"] for c in stub.posts()] == ["https://grok.example/v1/responses"]


def test_search_passes_options_through_shared_orchestration(monkeypatch):
    config = _base_config(
        provider_settings={
            "model": "grok-global",
            "detailed_model": "grok-detailed",
            "deep_model": "grok-deep",
        }
    )
    plugin, stub = _initialized_plugin(monkeypatch, config)
    stub.route("/v1/chat/completions", _chat_reply())
    svc = plugin.get_service()

    result = asyncio.run(
        svc.search(
            "q",
            search_depth="advanced",
            max_results=12,
            topic="news",
            days=3,
            system_prompt="Custom SDK prompt",
        )
    )
    assert result["ok"] is True
    body = stub.posts()[-1]["json"]
    assert body["model"] == "grok-detailed"
    assert body["reasoning_effort"] == "medium"
    assert body["messages"][0]["content"] == "Custom SDK prompt"
    user_text = body["messages"][1]["content"]
    assert "Depth: advanced" in user_text
    assert "up to 12" in user_text
    assert "Topic: news" in user_text

    asyncio.run(svc.search("q", search_depth="deep"))
    body = stub.posts()[-1]["json"]
    assert body["model"] == "grok-deep"
    assert body["reasoning_effort"] == "high"
    assert body["reasoning_budget_tokens"] == 32000


def test_search_with_images_builds_multimodal_content(monkeypatch):
    plugin, stub = _initialized_plugin(monkeypatch)
    stub.route("/v1/chat/completions", _chat_reply())
    result = asyncio.run(plugin.get_service().search("出处", images=[_png_b64()]))
    assert result["ok"] is True
    content = stub.posts()[-1]["json"]["messages"][1]["content"]
    assert isinstance(content, list)


def test_search_does_not_retry_even_when_config_enables_it(monkeypatch):
    config = _base_config(
        request_settings={"max_retries": 5, "retryable_status_codes": [500]}
    )
    plugin, stub = _initialized_plugin(monkeypatch, config)
    stub.route("/v1/chat/completions", _Response("upstream boom", 500))
    result = asyncio.run(plugin.get_service().search("q"))
    assert result["ok"] is False
    assert "500" in result["error"]
    assert len(stub.posts()) == 1  # SDK 固定 use_retry=False


def test_search_rejects_when_not_configured_or_invalid_params(monkeypatch):
    config = _base_config(
        connection_settings={"base_url": "", "api_key": ""},
        reverse_image_search={"serpapi_api_key": "", "saucenao_api_key": ""},
    )
    plugin, _ = _initialized_plugin(monkeypatch, config)
    svc = plugin.get_service()
    with pytest.raises(public_api.PluginServiceError) as ei:
        asyncio.run(svc.search("q"))
    assert ei.value.code == "not_ready"


def test_search_param_validation(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)
    svc = plugin.get_service()
    for bad_query in ("", "   ", None, 123):
        with pytest.raises(public_api.PluginServiceError) as ei:
            asyncio.run(svc.search(bad_query))
        assert ei.value.code == "invalid_request"
    for bad_images in ({"a": 1}, [1, 2], "b64"):
        with pytest.raises(public_api.PluginServiceError) as ei:
            asyncio.run(svc.search("q", images=bad_images))
        assert ei.value.code == "invalid_request"


def test_business_calls_rejected_while_initializing(monkeypatch):
    plugin, _ = _make_plugin(monkeypatch)
    svc = plugin.get_service()
    with pytest.raises(public_api.PluginServiceError) as ei:
        asyncio.run(svc.search("q"))
    assert ei.value.code == "not_ready"
    with pytest.raises(public_api.PluginServiceError) as ei:
        asyncio.run(svc.fetch("https://example.org"))
    assert ei.value.code == "not_ready"


# ─── fetch：共享结构化入口，Tool 字符串 / SDK 字典 ─────────────


def test_sdk_fetch_returns_full_api_dict(monkeypatch):
    plugin, stub = _initialized_plugin(monkeypatch)
    stub.route("/v1/chat/completions", _fetch_reply())
    result = asyncio.run(plugin.get_service().fetch("https://example.org/article"))
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["content"] == PAGE  # 完整原字典，不截断、不反解析字符串
    assert result["model"] == "srv-model"
    assert result["usage"] == {"total_tokens": 7}
    assert "elapsed_ms" in result


def test_sdk_fetch_respects_enable_fetch_without_network(monkeypatch):
    config = _base_config(tool_settings={"enable_fetch": False})
    plugin, stub = _initialized_plugin(monkeypatch, config)
    svc = plugin.get_service()
    with pytest.raises(public_api.PluginServiceError) as ei:
        asyncio.run(svc.fetch("https://example.org/article"))
    assert ei.value.code == "feature_disabled"
    assert stub.posts() == []


def test_sdk_fetch_invalid_url_returns_structured_error(monkeypatch):
    plugin, stub = _initialized_plugin(monkeypatch)
    result = asyncio.run(plugin.get_service().fetch("not-a-url"))
    assert result["ok"] is False
    assert result["error_kind"] == "invalid_url"
    assert stub.posts() == []


def test_sdk_fetch_reports_invalid_extension_config(monkeypatch):
    config = _base_config(advanced_settings={"extra_headers": "not-json"})
    plugin, stub = _initialized_plugin(monkeypatch, config)
    result = asyncio.run(plugin.get_service().fetch("https://example.org/article"))
    assert result["ok"] is False
    assert result["error_kind"] == "invalid_config"
    assert stub.posts() == []


def test_tool_and_sdk_share_fetch_entry_with_matching_semantics(monkeypatch):
    plugin, stub = _initialized_plugin(monkeypatch)
    stub.route("/v1/chat/completions", _fetch_reply())
    svc = plugin.get_service()

    sdk_dict = asyncio.run(svc.fetch("https://example.org/article"))
    tool_str = asyncio.run(plugin.grok_fetch_tool(object(), "https://example.org/a"))
    assert sdk_dict["ok"] is True and sdk_dict["content"] == PAGE
    assert tool_str == PAGE  # Tool 展示原字符串
    assert len(stub.posts()) == 2  # 各一次真实请求，同一共享入口

    stub.route("/v1/chat/completions", _Response("denied", 401))
    tool_err = asyncio.run(plugin.grok_fetch_tool(object(), "https://example.org/a"))
    assert tool_err.startswith("网页抓取失败") and "401" in tool_err
    sdk_err = asyncio.run(svc.fetch("https://example.org/a"))
    assert sdk_err["ok"] is False
    assert sdk_err["status"] == 401


def test_fetch_error_string_matches_legacy_tool_contract(monkeypatch):
    plugin, _ = _initialized_plugin(
        monkeypatch, _base_config(advanced_settings={"extra_body": "{bad"})
    )
    tool_str = asyncio.run(plugin.grok_fetch_tool(object(), "https://example.org/a"))
    assert tool_str.startswith("错误：扩展参数配置无效（")
    url_str = asyncio.run(plugin.grok_fetch_tool(object(), "ftp://bad"))
    assert url_str == "错误：请提供完整的 HTTP/HTTPS URL"


# ─── reverse_image_search：后端选择与默认关闭 ─────────────────


def _install_fake_backends(monkeypatch, calls):
    async def fake_serpapi(image, *, api_key, timeout, proxy=None):
        calls.append(("serpapi", api_key))
        return {
            "ok": True,
            "payload": {
                "visual_matches": [
                    {
                        "title": "Match",
                        "link": "https://example.org/m",
                        "source": "Lens",
                    }
                ]
            },
            "error": "",
        }

    async def fake_saucenao(image, *, api_key, timeout, proxy=None):
        calls.append(("saucenao", api_key))
        return {
            "ok": True,
            "payload": {
                "results": [
                    {
                        "header": {"similarity": 90.1, "index_name": "Pixiv"},
                        "data": {
                            "title": "Art",
                            "ext_urls": ["https://example.org/art"],
                        },
                    }
                ]
            },
            "error": "",
        }

    monkeypatch.setattr(main_mod, "serpapi_lens_search", fake_serpapi)
    monkeypatch.setattr(main_mod, "saucenao_search", fake_saucenao)


def test_reverse_image_search_runs_selected_backend_only(monkeypatch):
    plugin, stub = _initialized_plugin(monkeypatch)
    calls: list = []
    _install_fake_backends(monkeypatch, calls)
    agg = asyncio.run(
        plugin.get_service().reverse_image_search([_png_b64()], use_serpapi=True)
    )
    assert [name for name, _ in calls] == ["serpapi"]
    assert agg["requested"] is True
    assert agg["serpapi"]["ok"] is True
    assert agg["serpapi"]["matches"][0]["url"] == "https://example.org/m"
    assert agg["saucenao"]["ok"] is False
    assert "SerpAPI candidates" in agg["evidence_text"]
    assert stub.posts() == []  # 搜图不走 Grok API


def test_reverse_image_search_defaults_do_not_enable_paid_backends(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)
    calls: list = []
    _install_fake_backends(monkeypatch, calls)
    agg = asyncio.run(plugin.get_service().reverse_image_search([_png_b64()]))
    assert calls == []
    assert agg["requested"] is False
    assert agg["evidence_text"] == ""


def test_reverse_image_search_concurrent_backend_selection(monkeypatch):
    config = _base_config(
        reverse_image_search={
            "serpapi_api_key": "serp-fixture-key",
            "saucenao_api_key": "sauce-fixture-key",
        }
    )
    plugin, _ = _initialized_plugin(monkeypatch, config)
    calls: list = []
    _install_fake_backends(monkeypatch, calls)
    agg = asyncio.run(
        plugin.get_service().reverse_image_search(
            [_png_b64()], use_serpapi=True, use_saucenao=True
        )
    )
    assert sorted(name for name, _ in calls) == ["saucenao", "serpapi"]
    assert agg["serpapi"]["ok"] is True and agg["saucenao"]["ok"] is True


def test_reverse_image_search_skips_backend_without_key(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)  # 仅配置 SerpAPI Key
    calls: list = []
    _install_fake_backends(monkeypatch, calls)
    agg = asyncio.run(
        plugin.get_service().reverse_image_search([_png_b64()], use_saucenao=True)
    )
    assert calls == []  # 不擅自开启未配置的收费后端
    assert any("SauceNAO" in note for note in agg["notes"])


def test_reverse_image_search_requires_configuration_and_valid_params(monkeypatch):
    config = _base_config(
        connection_settings={"base_url": "", "api_key": ""},
        reverse_image_search={"serpapi_api_key": "", "saucenao_api_key": ""},
    )
    plugin, _ = _initialized_plugin(monkeypatch, config)
    svc = plugin.get_service()
    with pytest.raises(public_api.PluginServiceError) as ei:
        asyncio.run(svc.reverse_image_search([_png_b64()], use_serpapi=True))
    assert ei.value.code == "not_ready"

    full = _initialized_plugin(monkeypatch)[0]
    svc2 = full.get_service()
    for kwargs in ({"use_serpapi": "yes"}, {"use_saucenao": 1}):
        with pytest.raises(public_api.PluginServiceError) as ei:
            asyncio.run(svc2.reverse_image_search([], **kwargs))
        assert ei.value.code == "invalid_request"
    with pytest.raises(public_api.PluginServiceError) as ei:
        asyncio.run(svc2.reverse_image_search("b64-string"))
    assert ei.value.code == "invalid_request"


# ─── 生命周期：关闭、旧引用失效 ───────────────────────────────


def test_terminate_closes_service_and_rejects_business_calls(monkeypatch):
    plugin, _ = _initialized_plugin(monkeypatch)
    svc = plugin.get_service()
    asyncio.run(plugin.terminate())

    status = svc.get_status()  # 关闭后仍可查询诊断
    assert status["state"] == "closed"
    assert status["ready"] is False
    assert status["reason"] == "service_closed"

    for coro in (
        svc.search("q"),
        svc.fetch("https://example.org"),
        svc.reverse_image_search([]),
    ):
        with pytest.raises(public_api.PluginServiceError) as ei:
            asyncio.run(coro)
        assert ei.value.code == "service_closed"


def test_old_facade_not_reactivated_by_new_plugin_instance(monkeypatch):
    plugin_a, _ = _initialized_plugin(monkeypatch)
    svc_a = plugin_a.get_service()
    asyncio.run(plugin_a.terminate())

    plugin_b, _ = _initialized_plugin(monkeypatch)
    svc_b = plugin_b.get_service()

    assert svc_a.instance_id != svc_b.instance_id
    assert svc_a.get_status()["state"] == "closed"
    fresh = svc_b.get_status()
    assert fresh["state"] == "ready" and fresh["ready"] is True


def test_initialize_runs_intercepted_connectivity_check_then_ready(monkeypatch):
    plugin, stub = _make_plugin(monkeypatch)
    asyncio.run(plugin.initialize())
    # 真实 initialize 保留了启动连通性检查，且被替身拦截（未触网）
    assert [c["url"] for c in stub.gets()] == ["https://grok.example/v1/models"]
    status = plugin.get_service().get_status()
    assert status["state"] == "ready"
    # 状态查询不重复触发连通性检查
    assert len(stub.gets()) == 1


@pytest.mark.parametrize("document", ["README.md", "docs/plugin-api.md"])
@pytest.mark.parametrize(
    "state", ["missing", "disabled", "no_instance", "old_api", "ready"]
)
def test_documented_discovery_with_native_context(monkeypatch, document, state):
    import ast
    import importlib
    import re
    from types import SimpleNamespace

    from astrbot.api.star import Context
    from astrbot.core.star.star import StarMetadata
    from conftest import ROOT

    text = (ROOT / document).read_text(encoding="utf-8")
    block = next(
        block
        for block in re.findall(r"```python\n(.*?)```", text, re.S)
        if "def get_grok_service(" in block
    )
    helper = next(
        node for node in ast.parse(block).body if isinstance(node, ast.FunctionDef)
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[helper], type_ignores=[]), document, "exec"), namespace
    )

    plugin, _ = _initialized_plugin(monkeypatch)
    meta = StarMetadata(name="astrbot_plugin_grok_web_search", star_cls=plugin)
    if state == "disabled":
        meta.activated = False
    elif state == "no_instance":
        meta.star_cls = None
    elif state == "old_api":
        meta.star_cls = SimpleNamespace(get_service=None)
    context_module = importlib.import_module("astrbot.core.star.context")
    monkeypatch.setattr(
        context_module, "star_registry", [] if state == "missing" else [meta]
    )
    context = object.__new__(Context)
    if state == "ready":
        service = namespace["get_grok_service"](context)
        assert service is plugin.get_service()
        asyncio.run(plugin.terminate())
        assert service.get_status()["state"] == "closed"
    else:
        with pytest.raises(RuntimeError):
            namespace["get_grok_service"](context)
