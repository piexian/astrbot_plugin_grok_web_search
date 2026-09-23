"""Skill CLI 与插件共享核心的一致性、输出契约与配置修复回归。"""

import importlib.util
import json
import os
import sys

import pytest
from conftest import ROOT, load

tool = load("tool.tool")
PAGE = '# Article\n```json\n{"content":"example","sources":[]}\n```\nFinal paragraph.'


@pytest.fixture
def skill(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("GROK_"):
            monkeypatch.delenv(key)
    spec = importlib.util.spec_from_file_location(
        "grok_search_script_under_test", ROOT / "skill" / "scripts" / "grok_search.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_load_astrbot_plugin_config", lambda: ({}, ""))
    monkeypatch.setattr(
        mod, "_default_skill_config_paths", lambda: [str(tmp_path / "none.json")]
    )
    monkeypatch.setattr(
        mod, "_default_user_config_path", lambda: str(tmp_path / "user-none.json")
    )
    monkeypatch.setattr(mod, "_run_reverse_image_search_sync", lambda *args: {})

    def unexpected_network(*args, **kwargs):
        raise AssertionError("Unexpected network request in Skill tests")

    # 共享 api 包的 aiohttp 是唯一的网络出口；在构造期拦截，避免遗留未关闭会话
    import aiohttp

    real_init = aiohttp.ClientSession.__init__

    def no_network(self, *args, **kwargs):
        raise AssertionError("Unexpected network request in Skill tests")

    monkeypatch.setattr(aiohttp.ClientSession, "__init__", no_network)
    monkeypatch.setattr(aiohttp.ClientSession, "__aenter__", real_init, raising=False)
    return mod


def _api_modules():
    import api.grok_chat as api_chat
    import api.grok_responses as api_resp

    return api_chat, api_resp


def _configure(skill, monkeypatch, responses=False, custom=""):
    config = {
        "connection_settings": {
            "base_url": "https://example.invalid",
            "api_key": "test-fixture",
        },
        "provider_settings": {"model": "fixture-model", "use_responses_api": responses},
        "request_settings": {"custom_system_prompt": custom},
    }
    monkeypatch.setattr(skill, "_load_astrbot_plugin_config", lambda: (config, "OK"))
    return config


def _responses(message, citations=None):
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": message}],
            }
        ],
        "citations": citations or [],
        "usage": {"total_tokens": 123},
    }


def _chat(message):
    return {
        "choices": [{"message": {"content": message}}],
        "usage": {"total_tokens": 123},
    }


def _run(skill, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["grok_search.py", *args])
    rc = skill.main()
    captured = capsys.readouterr()
    return rc, json.loads(captured.out), captured.err


def test_missing_query_exits_before_reverse_search(skill, monkeypatch, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    calls = []

    def fake_reverse_search(args, config, images):
        calls.append(list(images))
        return {}

    monkeypatch.setattr(skill, "_run_reverse_image_search_sync", fake_reverse_search)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "grok_search.py",
            "--saucenao",
            "--image-files",
            str(img),
            "--base-url",
            "https://example.invalid",
            "--api-key",
            "test-fixture",
        ],
    )
    assert skill.main() == 2
    assert calls == []


def test_skill_accepts_depth_alias(skill, monkeypatch, capsys):
    for flag in ("--depth", "--depth=deep", "--search-depth", "--search-depth=deep"):
        parts = flag.split("=") if "=" in flag else [flag, "advanced"]
        monkeypatch.setattr(sys, "argv", ["grok_search.py", *parts])
        assert skill.main() == 2
        err = capsys.readouterr().err
        assert "unrecognized" not in err, flag
        assert "Missing base URL" in err, flag


@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("output", ["json", "llm"])
@pytest.mark.parametrize("custom", ["", "   ", " Custom rules "])
def test_search_config_prompt_and_output(
    skill, monkeypatch, capsys, responses, output, custom
):
    _configure(skill, monkeypatch, responses, custom)
    api_chat, api_resp = _api_modules()
    calls = []
    source = {
        "url": "https://example.org/proof",
        "title": "Proof",
        "snippet": "Evidence",
    }

    async def chat(**kwargs):
        calls.append(("chat", kwargs))
        return {
            "ok": True,
            "content": "Answer",
            "sources": [source],
            "raw": "",
            "usage": {"total_tokens": 123},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    async def resp(**kwargs):
        calls.append(("responses", kwargs))
        return {
            "ok": True,
            "content": "Answer",
            "sources": [source],
            "raw": "",
            "usage": {"total_tokens": 123},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(api_chat, "grok_search", chat)
    monkeypatch.setattr(api_resp, "grok_responses_search", resp)
    rc, out, _ = _run(
        skill, monkeypatch, capsys, "--query", "Question", "--output", output
    )
    assert rc == 0
    kind, sent = calls[0]
    assert kind == ("responses" if responses else "chat")
    assert sent["system_prompt"] == tool.resolve_system_prompt(
        custom, tool.DEFAULT_JSON_SYSTEM_PROMPT
    )
    # 搜索引导与时间约束由共享编排注入（general 无时间参数时无约束片段）
    assert sent["query"].endswith("[User query]\nQuestion")
    assert "Depth: basic" in sent["query"]  # 默认深度
    assert out["content"] == "Answer" and out["sources"] == [source]
    if output == "llm":
        assert set(out) == {"ok", "content", "sources"}
    else:
        assert out["usage"] == {"total_tokens": 123}
        assert {"query", "model", "config_path", "raw", "elapsed_ms"} <= out.keys()


def test_explicit_model_overrides_mode_defaults(skill, monkeypatch, capsys):
    """显式 --model 优先于模式专用模型与全局模型（此前被 quick_model 覆盖）。"""
    config = _configure(skill, monkeypatch)
    config["provider_settings"]["quick_model"] = "quick-x"
    config["provider_settings"]["detailed_model"] = "detailed-x"
    api_chat, _ = _api_modules()
    models = []

    async def chat(**kwargs):
        models.append(kwargs["model"])
        return _chat("Answer")

    monkeypatch.setattr(api_chat, "grok_search", chat)
    _run(
        skill,
        monkeypatch,
        capsys,
        "--query",
        "Question",
        "--output",
        "llm",
        "--model",
        "cli-model",
    )
    assert models == ["cli-model"]
    _run(skill, monkeypatch, capsys, "--query", "Question", "--output", "llm")
    assert models[-1] == "quick-x"  # 未显式指定时按模式取 quick_model
    _run(
        skill,
        monkeypatch,
        capsys,
        "--query",
        "Question",
        "--output",
        "llm",
        "--depth",
        "deep",
    )
    assert models[-1] == "fixture-model"  # deep_model 未配置时回退全局模型


def test_extra_json_text_config_dict_and_protected_headers(skill, monkeypatch, capsys):
    """插件 extra_body/extra_headers 的 JSON 文本配置被解析；受保护头不被覆盖。"""
    config = _configure(skill, monkeypatch)
    config["advanced_settings"] = {
        "extra_body": '{"temperature": 0.5}',
        "extra_headers": '{"Authorization": "Bearer evil", "X-Extra": "v"}',
    }
    api_chat, _ = _api_modules()
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return self._payload

    class Session:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            captured.update(kwargs)
            return Response(self._payload)

    monkeypatch.setattr(
        api_chat.aiohttp, "ClientSession", lambda: Session(_compact(_chat("Answer")))
    )
    rc, out, _ = _run(
        skill, monkeypatch, capsys, "--query", "Question", "--output", "llm"
    )
    assert rc == 0 and out["content"] == "Answer"
    body = captured["json"]
    assert body["temperature"] == 0.5  # JSON 文本配置生效
    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer test-fixture"  # 受保护头不被覆盖
    assert headers["X-Extra"] == "v"
    assert headers["Content-Type"] == "application/json"


def _compact(data):
    return json.dumps(data)


@pytest.mark.parametrize("mode", ["search", "fetch"])
@pytest.mark.parametrize("source", ["cli", "env"])
def test_overrides_visible_in_real_request(skill, monkeypatch, capsys, mode, source):
    """Mock transport 层看到最终覆盖值：请求 URL 与 Authorization 头。"""
    _configure(skill, monkeypatch)
    api_chat, _ = _api_modules()
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return self._payload

    class Session:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response(self._payload)

    monkeypatch.setattr(
        api_chat.aiohttp, "ClientSession", lambda: Session(_compact(_chat("Answer")))
    )
    extra = (
        ["--fetch-url", "https://example.org"] if mode == "fetch" else ["--query", "Q"]
    )
    expected_base = "cli-endpoint.invalid"
    expected_key = "cli-key-fixture"
    if source == "env":
        monkeypatch.setenv("GROK_BASE_URL", f"https://{expected_base}")
        monkeypatch.setenv("GROK_API_KEY", expected_key)
        override_args: list[str] = []
    else:
        override_args = [
            "--base-url",
            f"https://{expected_base}",
            "--api-key",
            expected_key,
        ]
    rc, out, _ = _run(
        skill, monkeypatch, capsys, *extra, "--output", "llm", *override_args
    )
    assert rc == 0 and out["ok"] is True
    assert captured["url"].startswith(f"https://{expected_base}/v1/")
    assert captured["headers"]["Authorization"] == f"Bearer {expected_key}"


def test_proxy_is_used_for_search(skill, monkeypatch, capsys):
    """Skill 搜索请求同样使用插件代理配置（此前仅反向搜图接入代理）。"""
    config = _configure(skill, monkeypatch)
    config["connection_settings"]["proxy"] = "http://127.0.0.1:7890"
    api_chat, _ = _api_modules()
    proxies = []

    async def chat(**kwargs):
        proxies.append(kwargs["proxy"])
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(api_chat, "grok_search", chat)
    rc, out, _ = _run(skill, monkeypatch, capsys, "--query", "Q", "--output", "llm")
    assert rc == 0
    assert proxies == ["http://127.0.0.1:7890"]


@pytest.mark.parametrize("output", ["json", "llm"])
def test_fetch_uses_chat_and_preserves_markdown(skill, monkeypatch, capsys, output):
    _configure(skill, monkeypatch, responses=True, custom="Search-only custom prompt")
    api_chat, api_resp = _api_modules()
    calls = []
    message = (
        "[ GROK DATA STREAM :: FETCH ]\n" + PAGE + "\nMODEL :: fixture\n1s · 5 tokens"
    )

    async def fetch(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "content": tool.strip_stream_decorations(message),
            "elapsed_ms": 5,
            "usage": {"total_tokens": 123},
            "model": "fixture-model",
        }

    monkeypatch.setattr(api_chat, "grok_fetch", fetch)

    def wrong_endpoint(**kwargs):
        pytest.fail("Fetch must not use the search pipeline")

    monkeypatch.setattr(api_resp, "grok_responses_search", wrong_endpoint)
    rc, out, _ = _run(
        skill,
        monkeypatch,
        capsys,
        "--fetch-url",
        "https://example.org/article",
        "--output",
        output,
    )
    print("DBG", rc, json.dumps(out) if isinstance(out, dict) else out)
    assert rc == 0 and out["ok"] is True
    assert out["content"] == PAGE
    assert out["fetch_url"] == "https://example.org/article"
    assert calls[0]["max_retries"] == 0  # Skill fetch 不自动重试
    if output == "llm":
        assert set(out) == {"ok", "content", "fetch_url"}
    else:
        assert out["usage"]["total_tokens"] == 123 and "elapsed_ms" in out


@pytest.mark.parametrize("output", ["json", "llm"])
@pytest.mark.parametrize("failure", ["http", "request", "api", "empty"])
def test_failure_output_keeps_status_not_raw_diagnostics(
    skill, monkeypatch, capsys, output, failure
):
    _configure(skill, monkeypatch)
    evidence = "Candidate evidence remains available"
    monkeypatch.setattr(
        skill,
        "_run_reverse_image_search_sync",
        lambda *args: {"evidence_text": evidence},
    )

    results = {
        "http": {
            "ok": False,
            "error": "HTTP 401 - 认证失败，请检查 api_key 是否正确",
            "status": 401,
            "error_kind": "http",
            "raw": "raw-debug",
        },
        "api": {
            "ok": False,
            "error": "API 返回错误: raw-debug",
            "error_kind": "api",
            "raw": "raw-debug",
        },
        "empty": {
            "ok": False,
            "error": "API 返回了空响应，请稍后重试",
            "error_kind": "empty",
            "raw": "",
        },
    }

    async def request(get_cfg=None, **kwargs):
        if failure == "request":
            raise ValueError("raw-debug")
        return results[failure]

    monkeypatch.setattr(skill, "_run_search", request)
    rc, out, _ = _run(
        skill, monkeypatch, capsys, "--query", "Question", "--output", output
    )
    assert rc == 1 and out["ok"] is False
    assert (
        out["error"]
        == {
            "http": "HTTP 401",
            "request": "request_failed",
            "api": "api_error",
            "empty": "empty_response",
        }[failure]
    )
    if output == "llm":
        assert set(out) == {"ok", "error", "evidence"}
        assert out["evidence"] == evidence
        assert "raw-debug" not in json.dumps(out)
    else:
        assert "elapsed_ms" in out and "detail" in out


@pytest.mark.parametrize(
    "message",
    [
        json.dumps(
            {
                "content": "[ GROK_DATA_STREAM ]\nAnswer\nMODEL :: fixture",
                "sources": [
                    {"url": " https://example.org/proof ", "title": "First"},
                    {"url": "https://example.org/proof", "title": "Duplicate"},
                    {"url": " "},
                    "invalid",
                ],
            }
        ),
        "[ GROK DATA STREAM ]\nAnswer https://example.org/proof\nMODEL :: fixture\n1s · 8 tokens",
    ],
)
def test_skill_search_parsing_matches_plugin(skill, monkeypatch, capsys, message):
    """解析走共享 parse_sources_from_message，Skill 与插件输出天然一致。"""
    _configure(skill, monkeypatch)
    api_chat, _ = _api_modules()

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return self._payload

    class Session:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            return Response(_compact(_chat(message)))

    monkeypatch.setattr(
        api_chat.aiohttp, "ClientSession", lambda: Session(_compact(_chat(message)))
    )
    rc, out, _ = _run(
        skill, monkeypatch, capsys, "--query", "Question", "--output", "llm"
    )
    expected = tool.parse_sources_from_message(message)
    assert rc == 0
    assert out["content"] == expected["content"]
    assert out["sources"] == expected["sources"]
    assert "raw" not in out


def test_responses_citations_are_preserved_and_deduplicated(skill, monkeypatch, capsys):
    _configure(skill, monkeypatch, responses=True)
    api_chat, api_resp = _api_modules()

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return self._payload

    class Session:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            return Response(
                _compact(
                    _responses(
                        '{"content":"Answer","sources":[]}',
                        ["https://example.org/proof", "https://example.org/proof"],
                    )
                )
            )

    monkeypatch.setattr(
        api_resp.aiohttp,
        "ClientSession",
        lambda: Session(
            _compact(
                _responses(
                    '{"content":"Answer","sources":[]}',
                    ["https://example.org/proof", "https://example.org/proof"],
                )
            )
        ),
    )
    monkeypatch.setattr(api_chat, "grok_search", lambda **kw: pytest.fail("no chat"))
    rc, out, _ = _run(
        skill, monkeypatch, capsys, "--query", "Question", "--output", "llm"
    )
    assert rc == 0
    assert out["sources"] == [
        {"url": "https://example.org/proof", "title": "", "snippet": ""}
    ]


def test_llm_image_output_keeps_candidates_not_backend_diagnostics(
    skill, monkeypatch, capsys
):
    _configure(skill, monkeypatch)
    agg = {
        "requested": True,
        "serpapi": {
            "matches": [{"title": "Artwork", "url": "https://example.org/art"}],
            "elapsed_ms": 999,
            "usage": {"tokens": 123},
        },
        "saucenao": {},
        "notes": ["SauceNAO unavailable"],
    }
    agg["evidence_text"] = skill.format_evidence(agg)
    monkeypatch.setattr(skill, "_run_reverse_image_search_sync", lambda *args: agg)

    async def search(get_cfg=None, **kw):
        return {
            "ok": True,
            "content": "Unconfirmed",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(skill, "_run_search", search)
    rc, out, _ = _run(
        skill, monkeypatch, capsys, "--query", "Find source", "--output", "llm"
    )
    assert rc == 0
    assert "https://example.org/art" in out["evidence"]
    assert "非确认结论" in out["evidence"]
    assert "SauceNAO unavailable" in out["evidence"]
    assert "reverse_image_search" not in out
    assert "999" not in json.dumps(out) and "tokens" not in json.dumps(out)


def test_default_output_remains_legacy_json(skill, monkeypatch, capsys):
    _configure(skill, monkeypatch)

    async def search(get_cfg=None, **kw):
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {"total_tokens": 1},
            "elapsed_ms": 2,
            "model": "fixture-model",
        }

    monkeypatch.setattr(skill, "_run_search", search)
    rc, out, _ = _run(skill, monkeypatch, capsys, "--query", "Question")
    assert rc == 0
    assert {"raw", "usage", "elapsed_ms", "config_path", "model"} <= out.keys()


@pytest.mark.parametrize("mode", ["search", "fetch"])
def test_cli_overrides_reach_api_endpoint_and_key(skill, monkeypatch, capsys, mode):
    """CLI --base-url/--api-key 必须实际进入请求，而非仅停留在局部变量。"""
    _configure(skill, monkeypatch)
    seen = {}

    async def capture(get_cfg=None, **kwargs):
        seen["base_url"] = get_cfg("base_url", "")
        seen["api_key"] = get_cfg("api_key", "")
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(skill, "_run_search", capture)
    monkeypatch.setattr(skill, "_run_fetch", capture)
    extra = (
        ["--fetch-url", "https://example.org"] if mode == "fetch" else ["--query", "Q"]
    )
    rc, out, _ = _run(
        skill,
        monkeypatch,
        capsys,
        *extra,
        "--output",
        "llm",
        "--base-url",
        "https://override.invalid",
        "--api-key",
        "override-fixture",
    )
    assert rc == 0 and out["ok"] is True
    assert seen["base_url"] == "https://override.invalid"
    assert seen["api_key"] == "override-fixture"
    # 密钥不得出现在 stdout/stderr 或诊断字段中
    assert "override-fixture" not in json.dumps(out)


@pytest.mark.parametrize("mode", ["search", "fetch"])
def test_env_overrides_reach_api_endpoint_and_key(skill, monkeypatch, capsys, mode):
    """GROK_BASE_URL / GROK_API_KEY 环境变量同样必须进入请求。"""
    _configure(skill, monkeypatch)
    monkeypatch.setenv("GROK_BASE_URL", "https://env.invalid")
    monkeypatch.setenv("GROK_API_KEY", "env-fixture")
    seen = {}

    async def capture(get_cfg=None, **kwargs):
        seen["base_url"] = get_cfg("base_url", "")
        seen["api_key"] = get_cfg("api_key", "")
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(skill, "_run_search", capture)
    monkeypatch.setattr(skill, "_run_fetch", capture)
    extra = (
        ["--fetch-url", "https://example.org"] if mode == "fetch" else ["--query", "Q"]
    )
    rc, out, _ = _run(skill, monkeypatch, capsys, *extra, "--output", "llm")
    assert rc == 0 and out["ok"] is True
    assert seen["base_url"] == "https://env.invalid"
    assert seen["api_key"] == "env-fixture"


def test_cli_overrides_beat_env_and_config(skill, monkeypatch, capsys):
    """优先级：CLI > env > 配置兜底。"""
    _configure(skill, monkeypatch)
    monkeypatch.setenv("GROK_BASE_URL", "https://env.invalid")
    monkeypatch.setenv("GROK_API_KEY", "env-fixture")
    seen = {}

    async def capture(get_cfg=None, **kwargs):
        seen["base_url"] = get_cfg("base_url", "")
        seen["api_key"] = get_cfg("api_key", "")
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(skill, "_run_search", capture)
    rc, _, _ = _run(
        skill,
        monkeypatch,
        capsys,
        "--query",
        "Q",
        "--output",
        "llm",
        "--base-url",
        "https://cli.invalid",
        "--api-key",
        "cli-fixture",
    )
    assert rc == 0
    assert seen["base_url"] == "https://cli.invalid"
    assert seen["api_key"] == "cli-fixture"


def test_plugin_config_used_when_no_cli_or_env(skill, monkeypatch, capsys):
    """无 CLI/env 覆盖时回落到插件配置（分组键）。"""
    _configure(skill, monkeypatch)
    seen = {}

    async def capture(get_cfg=None, **kwargs):
        seen["base_url"] = get_cfg("base_url", "")
        seen["api_key"] = get_cfg("api_key", "")
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(skill, "_run_search", capture)
    rc, _, _ = _run(skill, monkeypatch, capsys, "--query", "Q", "--output", "llm")
    assert rc == 0
    assert seen["base_url"] == "https://example.invalid"
    assert seen["api_key"] == "test-fixture"


@pytest.mark.parametrize("mode", ["search", "fetch"])
def test_cli_only_connection_starts_without_plugin_config(
    skill, monkeypatch, capsys, mode
):
    """仅靠 CLI 提供连接信息时也能启动（不依赖已存在配置）。"""
    monkeypatch.setattr(skill, "_load_astrbot_plugin_config", lambda: ({}, ""))
    seen = {}

    async def capture(get_cfg=None, **kwargs):
        seen["base_url"] = get_cfg("base_url", "")
        seen["api_key"] = get_cfg("api_key", "")
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(skill, "_run_search", capture)
    monkeypatch.setattr(skill, "_run_fetch", capture)
    extra = (
        ["--fetch-url", "https://example.org"] if mode == "fetch" else ["--query", "Q"]
    )
    rc, out, _ = _run(
        skill,
        monkeypatch,
        capsys,
        *extra,
        "--output",
        "llm",
        "--base-url",
        "https://cli-only.invalid",
        "--api-key",
        "cli-only-fixture",
    )
    assert rc == 0 and out["ok"] is True
    assert seen["base_url"] == "https://cli-only.invalid"
    assert seen["api_key"] == "cli-only-fixture"


def test_persistent_skill_config_is_fallback_for_installed_script(
    monkeypatch, capsys, tmp_path
):
    """安装态缺失本地 config.json 时，回落到 plugin_data 持久化 skill 配置。"""
    for key in list(os.environ):
        if key.startswith("GROK_"):
            monkeypatch.delenv(key)
    spec = importlib.util.spec_from_file_location(
        "grok_search_persistent_test", ROOT / "skill" / "scripts" / "grok_search.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    data_root = tmp_path / "data"
    persistent = data_root / "plugin_data" / "astrbot_plugin_grok_web_search" / "skill"
    persistent.mkdir(parents=True)
    (persistent / "config.json").write_text(
        '{"base_url": "https://persistent.invalid", "api_key": "persistent-fixture"}',
        encoding="utf-8",
    )
    installed_root = tmp_path / "skills" / "grok-search"
    installed_root.mkdir(parents=True)

    monkeypatch.setattr(mod, "_find_astrbot_data_path", lambda: str(data_root))
    monkeypatch.setattr(mod, "_skill_root", lambda: str(installed_root))
    monkeypatch.setattr(mod, "_load_astrbot_plugin_config", lambda: ({}, ""))
    monkeypatch.setattr(
        mod, "_default_user_config_path", lambda: str(tmp_path / "user-none.json")
    )
    monkeypatch.setattr(mod, "_run_reverse_image_search_sync", lambda *args: {})
    seen = {}

    async def capture(get_cfg=None, **kwargs):
        seen["base_url"] = get_cfg("base_url", "")
        seen["api_key"] = get_cfg("api_key", "")
        return {
            "ok": True,
            "content": "Answer",
            "sources": [],
            "raw": "",
            "usage": {},
            "elapsed_ms": 1,
            "model": "fixture-model",
        }

    monkeypatch.setattr(mod, "_run_search", capture)
    monkeypatch.setattr(
        sys, "argv", ["grok_search.py", "--query", "Q", "--output", "llm"]
    )
    rc = mod.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True
    assert seen["base_url"] == "https://persistent.invalid"
    assert seen["api_key"] == "persistent-fixture"


def test_missing_connection_still_reports_local_error(skill, monkeypatch, capsys):
    """CLI/env/配置都没有连接信息时仍是本地错误码 2，不发起请求。"""
    monkeypatch.setattr(skill, "_load_astrbot_plugin_config", lambda: ({}, ""))

    async def must_not_run(*args, **kwargs):
        pytest.fail("无连接信息时不得发起请求")

    monkeypatch.setattr(skill, "_run_search", must_not_run)
    monkeypatch.setattr(sys, "argv", ["grok_search.py", "--query", "Q"])
    assert skill.main() == 2
    assert "Missing base URL" in capsys.readouterr().err


def test_missing_dependency_reports_clear_error(skill, monkeypatch, capsys):
    """aiohttp 缺失时输出明确错误而不是堆栈；LLM 输出不含依赖细节。"""
    _configure(skill, monkeypatch)

    async def boom(get_cfg=None, **kwargs):
        raise ImportError("No module named 'aiohttp'")

    monkeypatch.setattr(skill, "_run_search", boom)
    rc, out, _ = _run(skill, monkeypatch, capsys, "--query", "Q", "--output", "json")
    assert rc == 1 and out["ok"] is False
    assert out["error"] == "request_failed"
    assert "aiohttp" in out["detail"]


def test_unexpected_network_blocked_by_guard(skill, monkeypatch, capsys):
    """fixture 的网络守卫本身可用：触发共享管道时直接失败而非静默联网。"""
    _configure(skill, monkeypatch)
    rc, out, _ = _run(skill, monkeypatch, capsys, "--query", "Q", "--output", "llm")
    assert rc == 1 and out["error"] == "request_failed"
