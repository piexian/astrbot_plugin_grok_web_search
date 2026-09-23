"""抓取保留 Markdown 原文，搜索继续解析 JSON 协议。"""

import asyncio
import json

import pytest
from conftest import load

chat = load("api.grok_chat")
tool = load("tool.tool")
PAGE = (
    "# API documentation\n\nExample:\n```json\n"
    '{"content":"example value","sources":[]}\n```\n\n'
    "Important explanation after the example."
)


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


class _Session:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _mock_http(monkeypatch, message, status=200, stream=False):
    calls = []
    if stream:
        payload = (
            "data: "
            + json.dumps({"choices": [{"delta": {"content": message}}]})
            + "\n\ndata: [DONE]\n"
        )
    else:
        payload = json.dumps({"choices": [{"message": {"content": message}}]})
    response = _Response(
        payload, status, "text/event-stream" if stream else "application/json"
    )
    monkeypatch.setattr(
        chat.aiohttp, "ClientSession", lambda: _Session(response, calls)
    )
    return calls


@pytest.mark.parametrize(
    "page",
    [PAGE, '{"content":"this is the page itself"}', "# Article\nOriginal text"],
)
@pytest.mark.parametrize("stream", [False, True])
def test_fetch_preserves_entire_document(monkeypatch, page, stream):
    calls = _mock_http(monkeypatch, page, stream=stream)
    result = asyncio.run(
        chat.grok_fetch(
            "https://example.org/article", "https://example.invalid", "test-fixture"
        )
    )
    assert result["ok"] is True
    assert result["content"] == page
    body = calls[0][1]["json"]
    assert body["messages"][0]["content"] == tool.FETCH_SYSTEM_PROMPT
    assert "parse_json_response" not in body


def test_fetch_strips_only_outer_runtime_decorations(monkeypatch):
    body = PAGE + "\n\nA benchmark used 20 tokens; model names are article content."
    message = (
        "[ GROK DATA STREAM :: FETCH ]\nSYS.STATUS: ONLINE\n\n"
        + body
        + "\n\nMODEL :: fixture\n1.0s · 20 tokens"
    )
    _mock_http(monkeypatch, message)
    result = asyncio.run(
        chat.grok_fetch(
            "https://example.org/article", "https://example.invalid", "test-fixture"
        )
    )
    assert result["content"] == body


@pytest.mark.parametrize("message", ["", "Access denied: page is unavailable."])
def test_fetch_does_not_fabricate_missing_page(monkeypatch, message):
    _mock_http(monkeypatch, message)
    result = asyncio.run(
        chat.grok_fetch(
            "https://example.org/article", "https://example.invalid", "test-fixture"
        )
    )
    if message:
        assert result["content"] == message
    else:
        assert result["ok"] is False
        assert "空响应" in result["error"] or "为空" in result["error"]


def test_fetch_http_failure_is_preserved_without_retry(monkeypatch):
    calls = _mock_http(monkeypatch, "Unauthorized", status=401)
    result = asyncio.run(
        chat.grok_fetch(
            "https://example.org/article", "https://example.invalid", "test-fixture"
        )
    )
    assert result["ok"] is False
    assert "401" in result["error"]
    assert len(calls) == 1


@pytest.mark.parametrize("custom", [None, "Custom rules"])
def test_search_still_parses_json_and_selects_prompt(monkeypatch, custom):
    message = json.dumps(
        {
            "content": "[ GROK DATA STREAM :: SEARCH ]\nAnswer\nMODEL :: fixture",
            "sources": [
                {"url": "https://example.org/1", "title": "Source"},
                {"url": "https://example.org/1", "title": "Duplicate"},
            ],
        }
    )
    calls = _mock_http(monkeypatch, message)
    result = asyncio.run(
        chat.grok_search(
            "Question", "https://example.invalid", "test-fixture", system_prompt=custom
        )
    )
    assert result["ok"] is True
    assert result["content"] == "Answer"
    assert len(result["sources"]) == 1
    assert result["raw"] == ""
    assert calls[0][1]["json"]["messages"][0]["content"] == (
        custom if custom is not None else tool.DEFAULT_JSON_SYSTEM_PROMPT
    )


@pytest.mark.parametrize("custom", [None, "Custom rules"])
def test_responses_search_uses_shared_prompt(monkeypatch, custom):
    responses = load("api.grok_responses")
    calls = []
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": '{"content":"Answer","sources":[]}'}
                ],
            }
        ]
    }
    response = _Response(json.dumps(payload))
    monkeypatch.setattr(
        responses.aiohttp, "ClientSession", lambda: _Session(response, calls)
    )
    result = asyncio.run(
        responses.grok_responses_search(
            "Question", "https://example.invalid", "test-fixture", system_prompt=custom
        )
    )
    assert result["ok"] is True and result["content"] == "Answer"
    assert calls[0][1]["json"]["input"][0]["content"] == (
        custom if custom is not None else tool.DEFAULT_JSON_SYSTEM_PROMPT
    )


def test_responses_duplicate_citations_collapse_to_single_source(monkeypatch):
    """annotations 与顶层 citations 的重复 URL 合并，插件只输出一个来源。"""

    responses = load("api.grok_responses")
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"content":"Answer","sources":[]}',
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.org/a",
                                "title": "A",
                            },
                            {
                                "type": "url_citation",
                                "url": "https://example.org/a",
                                "title": "A-dup",
                            },
                        ],
                    }
                ],
            }
        ],
        "citations": ["https://example.org/a", "https://example.org/b"],
    }
    response = _Response(json.dumps(payload))
    calls = []
    monkeypatch.setattr(
        responses.aiohttp, "ClientSession", lambda: _Session(response, calls)
    )
    result = asyncio.run(
        responses.grok_responses_search(
            "Question", "https://example.invalid", "test-fixture"
        )
    )
    assert result["ok"] is True
    assert [s["url"] for s in result["sources"]] == [
        "https://example.org/a",
        "https://example.org/b",
    ]
    assert [c["url"] for c in result["citations"]] == [
        "https://example.org/a",
        "https://example.org/b",
    ]


def test_error_results_carry_error_kind(monkeypatch):
    """错误结果带类别标记，供 Skill 旧诊断输出分类使用。"""
    _mock_http(monkeypatch, "Unauthorized", status=401)
    result = asyncio.run(
        chat.grok_search(
            "Question", "https://example.invalid", "test-fixture", max_retries=0
        )
    )
    assert result["ok"] is False
    assert result["error_kind"] == "http"
    assert result["status"] == 401

    _mock_http(monkeypatch, "")  # 空消息触发 empty 类别
    result = asyncio.run(
        chat.grok_search("Question", "https://example.invalid", "test-fixture")
    )
    assert result["ok"] is False
    assert result["error_kind"] == "empty"
