"""宿主通用请求头注入：合并优先级与四类请求覆盖回归。

main.py 在插件入口可选取得 AstrBot 通用头（如 UA）并注入共享请求层；
共享核心不依赖 AstrBot，接口缺失/失败时保持原行为。
"""

import ast
import asyncio
import copy
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from conftest import ROOT, load

tool = load("tool.tool")
chat_mod = load("api.grok_chat")
resp_mod = load("api.grok_responses")

HOST_UA = "astrbot/9.9.9-fixture"


@pytest.fixture(autouse=True)
def _reset_default_headers():
    tool.set_default_headers(None)
    yield
    tool.set_default_headers(None)


# ─── build_headers 合并语义 ──────────────────────────────────


def test_build_headers_priority_and_case_insensitive_merge():
    tool.set_default_headers({"User-Agent": HOST_UA, "X-Host": "host"})
    extra = {
        "user-agent": "user-agent-fixture",
        "X-Extra": "v",
        "authorization": "Bearer evil",
        "content-type": "text/plain",
    }
    extra_snapshot = copy.deepcopy(extra)
    headers = tool.build_headers("key-fixture", extra)

    assert headers["User-Agent"] == "user-agent-fixture"  # extra 覆盖宿主默认
    assert "user-agent" not in headers  # 大小写不敏感，不产生重复键
    assert headers["X-Host"] == "host"
    assert headers["X-Extra"] == "v"
    assert headers["Authorization"] == "Bearer key-fixture"  # 固定鉴权最高
    assert headers["Content-Type"] == "application/json"
    assert extra == extra_snapshot  # 不修改调用方字典


def test_build_headers_fixed_keys_win_over_host_defaults_case_insensitive():
    tool.set_default_headers(
        {"authorization": "Bearer host-evil", "CONTENT-TYPE": "text/plain"}
    )
    headers = tool.build_headers("key-fixture")
    assert headers["Authorization"] == "Bearer key-fixture"
    assert headers["Content-Type"] == "application/json"
    assert "authorization" not in headers and "CONTENT-TYPE" not in headers


def test_default_headers_are_stored_and_returned_as_copies():
    defaults = {"User-Agent": HOST_UA}
    tool.set_default_headers(defaults)
    defaults["User-Agent"] = "mutated-after-set"
    assert tool.get_default_headers() == {"User-Agent": HOST_UA}

    exposed = tool.get_default_headers()
    exposed["User-Agent"] = "mutated-result"
    assert tool.get_default_headers() == {"User-Agent": HOST_UA}


def test_build_headers_without_host_headers_keeps_previous_behavior():
    extra = {"X-Extra": "v"}
    assert tool.build_headers("k", extra) == {
        "Content-Type": "application/json",
        "Authorization": "Bearer k",
        "X-Extra": "v",
    }
    assert tool.build_headers("k") == {
        "Content-Type": "application/json",
        "Authorization": "Bearer k",
    }


# ─── 四类请求：最终发出的请求头 ──────────────────────────────


class _MockResponse:
    def __init__(self, status=200, body="", content_type="application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self._body


def _chat_body(message="Answer"):
    return json.dumps(
        {"choices": [{"message": {"content": message}}], "usage": {"total_tokens": 3}}
    )


def _responses_body(message="Answer"):
    return json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": message}],
                }
            ],
            "citations": [],
            "usage": {"total_tokens": 3},
        }
    )


def _install_session(monkeypatch, module, responses):
    """替换 aiohttp.ClientSession；responses(attempt, kwargs) 返回响应对象。"""
    captured = []
    state = {"attempt": 0}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            captured.append(kwargs)
            resp = responses(state["attempt"], kwargs)
            state["attempt"] += 1
            return resp

        def get(self, url, **kwargs):
            captured.append(kwargs)
            resp = responses(state["attempt"], kwargs)
            state["attempt"] += 1
            return resp

    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: Session())
    return captured


def _assert_fixed_headers(headers, extra=None):
    assert headers["User-Agent"] == HOST_UA
    assert headers["Authorization"] == "Bearer key-fixture"
    assert headers["Content-Type"] == "application/json"
    for key, value in (extra or {}).items():
        assert headers[key] == value


@pytest.mark.parametrize("stream", [False, True])
def test_chat_search_request_headers(monkeypatch, stream):
    tool.set_default_headers({"User-Agent": HOST_UA})
    if stream:
        body = 'data: {"choices":[{"delta":{"content":"Answer"}}]}\n\ndata: [DONE]'
        response = _MockResponse(body=body, content_type="text/event-stream")
    else:
        response = _MockResponse(body=_chat_body())
    captured = _install_session(monkeypatch, chat_mod, lambda attempt, kw: response)

    result = asyncio.run(
        chat_mod.grok_search(
            query="Q",
            base_url="https://example.invalid",
            api_key="key-fixture",
            max_retries=0,
            stream=stream,
            extra_headers={"X-Extra": "v"},
        )
    )
    assert result["ok"] is True
    assert len(captured) == 1
    _assert_fixed_headers(captured[0]["headers"], {"X-Extra": "v"})


def test_chat_search_retry_path_reuses_host_headers(monkeypatch):
    tool.set_default_headers({"User-Agent": HOST_UA})

    def responses(attempt, kwargs):
        if attempt == 0:
            return _MockResponse(status=429, body="rate limited")
        return _MockResponse(body=_chat_body())

    captured = _install_session(monkeypatch, chat_mod, responses)
    result = asyncio.run(
        chat_mod.grok_search(
            query="Q",
            base_url="https://example.invalid",
            api_key="key-fixture",
            max_retries=1,
            retry_delay=0.0,
        )
    )
    assert result["ok"] is True
    assert len(captured) == 2, "重试必须真实发生"
    for call in captured:
        _assert_fixed_headers(call["headers"])


def test_responses_search_request_headers(monkeypatch):
    tool.set_default_headers({"User-Agent": HOST_UA})
    response = _MockResponse(body=_responses_body())
    captured = _install_session(monkeypatch, resp_mod, lambda attempt, kw: response)

    result = asyncio.run(
        resp_mod.grok_responses_search(
            query="Q",
            base_url="https://example.invalid",
            api_key="key-fixture",
            max_retries=0,
        )
    )
    assert result["ok"] is True
    assert len(captured) == 1
    _assert_fixed_headers(captured[0]["headers"])


def test_fetch_request_headers(monkeypatch):
    tool.set_default_headers({"User-Agent": HOST_UA})
    response = _MockResponse(body=_chat_body("Page markdown"))
    captured = _install_session(monkeypatch, chat_mod, lambda attempt, kw: response)

    result = asyncio.run(
        chat_mod.grok_fetch(
            url="https://example.org/page",
            base_url="https://example.invalid",
            api_key="key-fixture",
            max_retries=0,
        )
    )
    assert result["ok"] is True
    assert len(captured) == 1
    _assert_fixed_headers(captured[0]["headers"])


def test_models_connectivity_check_headers():
    """main._validate_config 的 /v1/models 检查同样带宿主默认头。"""
    tool.set_default_headers({"User-Agent": HOST_UA})
    captured = []

    class _Timeout:
        def __init__(self, total=None):
            self.total = total

    class _ClientError(Exception):
        pass

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return "{}"

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            captured.append(kwargs)
            return _Resp()

    namespace = {
        "__name__": "validate_ns",
        "__package__": "probe",
        "asyncio": asyncio,
        "aiohttp": SimpleNamespace(
            ClientSession=lambda: _Session(),
            ClientTimeout=_Timeout,
            ClientError=_ClientError,
        ),
        "logger": SimpleNamespace(
            warning=lambda *a, **k: None, info=lambda *a, **k: None
        ),
        "PLUGIN_NAME": "probe",
        "normalize_base_url": tool.normalize_base_url,
        "normalize_api_key": tool.normalize_api_key,
        "build_headers": tool.build_headers,
    }
    plugin_cls = _exec_main_method("_validate_config", namespace)
    plugin = plugin_cls.__new__(plugin_cls)
    plugin._cfg = lambda key, default=None: {
        "base_url": "https://example.invalid",
        "api_key": "key-fixture",
        "proxy": "",
    }.get(key, default)
    plugin._parse_json_config = lambda key: {}

    asyncio.run(plugin._validate_config())

    assert len(captured) == 1
    _assert_fixed_headers(captured[0]["headers"])


# ─── 宿主接口缺失/异常不阻断 ────────────────────────────────


def _exec_main_method(name: str, namespace: dict):
    """摘取 main.py 的单个方法，注入 namespace 后编译为类（与现有测试同法）。"""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    method = copy.deepcopy(
        next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
    )
    method.decorator_list = []
    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                ast.ClassDef(
                    name="Plugin",
                    bases=[],
                    keywords=[],
                    body=[method],
                    decorator_list=[],
                ),
            ],
            type_ignores=[],
        )
    )
    exec(compile(module, f"main.py:{name}", "exec"), namespace)
    return namespace["Plugin"]


def _load_host_headers_function():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    func = copy.deepcopy(
        next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_load_host_default_headers"
        )
    )
    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                func,
            ],
            type_ignores=[],
        )
    )
    namespace = {"__name__": "host_headers_ns", "__package__": "probe"}
    exec(compile(module, "main.py:_load_host_default_headers", "exec"), namespace)
    return namespace["_load_host_default_headers"]


def test_host_headers_missing_interface_returns_empty(monkeypatch):
    loader = _load_host_headers_function()
    monkeypatch.setitem(sys.modules, "astrbot.core.provider.headers", None)
    assert loader() == {}


def test_host_headers_builder_failure_returns_empty(monkeypatch):
    loader = _load_host_headers_function()
    fake = SimpleNamespace(
        build_provider_headers=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setitem(sys.modules, "astrbot.core.provider.headers", fake)
    assert loader() == {}


def test_host_headers_non_dict_result_returns_empty(monkeypatch):
    loader = _load_host_headers_function()
    fake = SimpleNamespace(build_provider_headers=lambda: "not-a-dict")
    monkeypatch.setitem(sys.modules, "astrbot.core.provider.headers", fake)
    assert loader() == {}


def test_host_headers_success_returns_copy(monkeypatch):
    loader = _load_host_headers_function()
    fake = SimpleNamespace(build_provider_headers=lambda: {"User-Agent": HOST_UA})
    monkeypatch.setitem(sys.modules, "astrbot.core.provider.headers", fake)
    result = loader()
    assert result == {"User-Agent": HOST_UA}
    result["User-Agent"] = "mutated"
    assert loader() == {"User-Agent": HOST_UA}


# ─── 插件入口注入接线 ────────────────────────────────────────


def test_plugin_init_injects_host_headers():
    """__init__ 实际把可选取得的宿主头注入共享请求层。"""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    method = copy.deepcopy(
        next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
    )
    method.decorator_list = []

    class _StarShim:
        def __init__(self, context):
            self.context = context

    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                ast.ClassDef(
                    name="Plugin",
                    bases=[ast.Name(id="_StarShim", ctx=ast.Load())],
                    keywords=[],
                    body=[method],
                    decorator_list=[],
                ),
            ],
            type_ignores=[],
        )
    )
    namespace = {
        "__name__": "init_ns",
        "__package__": "probe",
        "threading": threading,
        "_StarShim": _StarShim,
        "_load_host_default_headers": lambda: {"User-Agent": HOST_UA},
        "set_default_headers": tool.set_default_headers,
    }
    exec(compile(module, "main.py:__init__", "exec"), namespace)
    plugin_cls = namespace["Plugin"]
    plugin_cls._migrate_legacy_config = lambda self: None

    plugin = plugin_cls(object(), {"base_url": "https://example.invalid"})

    assert tool.get_default_headers() == {"User-Agent": HOST_UA}
    assert plugin.config == {"base_url": "https://example.invalid"}
