"""Skill 配置、协议分支与面向模型的输出回归。"""

import importlib.util
import io
import json
import os
import sys
import urllib.error

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

    monkeypatch.setattr(mod.urllib.request, "urlopen", unexpected_network)
    return mod


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
@pytest.mark.parametrize("custom", [None, "", "  ", "  Custom rules  "])
def test_skill_request_prompt_matches_plugin(skill, monkeypatch, responses, custom):
    calls = []

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, **kwargs):
        calls.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(skill.urllib.request, "urlopen", urlopen)
    kwargs = {
        "base_url": "https://example.invalid",
        "api_key": "test-fixture",
        "model": "fixture-model",
        "query": "Question",
        "timeout_seconds": 1,
        "extra_headers": {},
        "extra_body": {},
        "system_prompt": custom,
    }
    if responses:
        skill._request_responses_api(**kwargs)
        messages = calls[0]["input"]
    else:
        skill._request_chat_completions(
            **kwargs, reasoning_effort=None, reasoning_budget_tokens=None
        )
        messages = calls[0]["messages"]
    expected = custom if custom is not None else tool.DEFAULT_JSON_SYSTEM_PROMPT
    assert messages[0]["content"] == expected


@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("output", ["json", "llm"])
@pytest.mark.parametrize("custom", ["", "   ", " Custom rules "])
def test_search_config_prompt_and_output(
    skill, monkeypatch, capsys, responses, output, custom
):
    _configure(skill, monkeypatch, responses, custom)
    calls = []
    source = {
        "url": "https://example.org/proof",
        "title": "Proof",
        "snippet": "Evidence",
    }
    message = json.dumps({"content": "Answer", "sources": [source]})

    def chat(**kwargs):
        calls.append(("chat", kwargs))
        return _chat(message)

    def resp(**kwargs):
        calls.append(("responses", kwargs))
        return _responses(message)

    monkeypatch.setattr(skill, "_request_chat_completions", chat)
    monkeypatch.setattr(skill, "_request_responses_api", resp)
    rc, out, _ = _run(
        skill, monkeypatch, capsys, "--query", "Question", "--output", output
    )
    assert rc == 0
    kind, sent = calls[0]
    assert kind == ("responses" if responses else "chat")
    assert sent["system_prompt"] == tool.resolve_system_prompt(
        custom, tool.DEFAULT_JSON_SYSTEM_PROMPT
    )
    assert sent["query"] == tool.build_search_query("Question", "basic", 7, "")
    assert out["content"] == "Answer" and out["sources"] == [source]
    if output == "llm":
        assert set(out) == {"ok", "content", "sources"}
    else:
        assert out["usage"] == {"total_tokens": 123}
        assert {"query", "model", "config_path", "raw", "elapsed_ms"} <= out.keys()


@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("output", ["json", "llm"])
def test_fetch_uses_chat_parser_and_preserves_markdown(
    skill, monkeypatch, capsys, responses, output
):
    _configure(skill, monkeypatch, responses, "Search-only custom prompt")
    calls = []
    message = (
        "[ GROK DATA STREAM :: FETCH ]\n" + PAGE + "\nMODEL :: fixture\n1s · 5 tokens"
    )

    def chat(**kwargs):
        calls.append(kwargs)
        return _chat(message)

    def wrong_endpoint(**kwargs):
        pytest.fail("Fetch must not use the Responses endpoint")

    monkeypatch.setattr(skill, "_request_chat_completions", chat)
    monkeypatch.setattr(skill, "_request_responses_api", wrong_endpoint)
    rc, out, _ = _run(
        skill,
        monkeypatch,
        capsys,
        "--fetch-url",
        "https://example.org/article",
        "--output",
        output,
    )
    assert rc == 0 and out["ok"] is True
    assert out["content"] == PAGE
    assert calls[0]["system_prompt"] == tool.FETCH_SYSTEM_PROMPT
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

    def request(**kwargs):
        if failure == "http":
            raise urllib.error.HTTPError(
                "https://example.invalid",
                401,
                "Unauthorized",
                {},
                io.BytesIO(b"raw-debug"),
            )
        if failure == "request":
            raise ValueError("raw-debug")
        if failure == "api":
            return {"error": {"message": "raw-debug"}}
        return _chat("")

    monkeypatch.setattr(skill, "_request_chat_completions", request)
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
    _configure(skill, monkeypatch)
    monkeypatch.setattr(skill, "_request_chat_completions", lambda **kw: _chat(message))
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
    monkeypatch.setattr(
        skill,
        "_request_responses_api",
        lambda **kw: _responses(
            '{"content":"Answer","sources":[]}',
            ["https://example.org/proof", "https://example.org/proof"],
        ),
    )
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
    monkeypatch.setattr(
        skill,
        "_request_chat_completions",
        lambda **kw: _chat('{"content":"Unconfirmed"}'),
    )
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
    monkeypatch.setattr(
        skill, "_request_chat_completions", lambda **kw: _chat("Answer")
    )
    rc, out, _ = _run(skill, monkeypatch, capsys, "--query", "Question")
    assert rc == 0
    assert {"raw", "usage", "elapsed_ms", "config_path", "model"} <= out.keys()
