"""Both real HTTP adapters share one retry budget for transient failures."""

import asyncio
import inspect
import json

import aiohttp
import pytest
from conftest import load


class _Response:
    def __init__(self, body, status=200, content_type="application/json"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.body


class _Session:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        index = len(self.calls)
        self.calls.append((url, kwargs))
        assert index < len(self.replies), "adapter exceeded the expected request budget"
        reply = self.replies[index]
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture(params=["chat", "responses"])
def adapter(request):
    name = request.param
    module = load(f"api.grok_{name}")
    search = module.grok_search if name == "chat" else module.grok_responses_search
    return name, module, search


def _payload(name, text):
    if name == "chat":
        return {"choices": [{"message": {"content": text}}]}
    return {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ]
    }


def _json_response(payload):
    return _Response(json.dumps(payload))


def _success(name):
    return _json_response(_payload(name, '{"content":"Answer","sources":[]}'))


def _empty(name, kind):
    if kind == "empty-body":
        return _Response("")
    if kind == "whitespace-body":
        return _Response(" \t\r\n ")
    if kind == "empty-final-text":
        return _json_response(_payload(name, ""))
    if kind == "whitespace-final-text":
        return _json_response(_payload(name, " \t\n "))
    return _json_response({"choices" if name == "chat" else "output": []})


def _run(monkeypatch, adapter, replies, max_retries, **options):
    _, module, search = adapter
    session = _Session(replies)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda: session)
    result = asyncio.run(
        search(
            query="Question",
            base_url="https://example.invalid",
            api_key="test-fixture",
            max_retries=max_retries,
            retry_delay=0,
            **options,
        )
    )
    return session.calls, result


def _assert_outcome(calls, result, *, ok, retries):
    assert len(calls) == retries + 1
    assert result["ok"] is ok
    assert result["content"] == ("Answer" if ok else "")
    assert result["retries"] == retries


def test_adapters_expose_only_the_shared_retry_policy(adapter):
    parameters = inspect.signature(adapter[2]).parameters
    assert "empty_response_retries" not in parameters
    assert parameters["retry_http"].default is True


@pytest.mark.parametrize(
    "kind",
    [
        "empty-body",
        "whitespace-body",
        "empty-final-text",
        "whitespace-final-text",
        "empty-output-list",
    ],
)
@pytest.mark.parametrize("retry_http", [True, False])
def test_empty_response_retries_then_succeeds(monkeypatch, adapter, kind, retry_http):
    name = adapter[0]
    calls, result = _run(
        monkeypatch,
        adapter,
        [_empty(name, kind), _success(name)],
        max_retries=1,
        retry_http=retry_http,
    )
    _assert_outcome(calls, result, ok=True, retries=1)


@pytest.mark.parametrize("max_retries", [0, 1, 2])
@pytest.mark.parametrize(
    "kind",
    ["empty-body", "whitespace-body", "empty-final-text", "whitespace-final-text"],
)
@pytest.mark.parametrize("retry_http", [True, False])
def test_empty_response_stops_at_total_budget(
    monkeypatch, adapter, kind, max_retries, retry_http
):
    name = adapter[0]
    replies = [_empty(name, kind) for _ in range(max_retries + 1)]
    replies.append(_success(name))
    calls, result = _run(
        monkeypatch, adapter, replies, max_retries, retry_http=retry_http
    )
    _assert_outcome(calls, result, ok=False, retries=max_retries)


@pytest.mark.parametrize(
    "body", ["not JSON", "{broken", "<html>upstream failure</html>"]
)
def test_malformed_nonempty_json_is_not_retried(monkeypatch, adapter, body):
    calls, result = _run(
        monkeypatch, adapter, [_Response(body), _success(adapter[0])], max_retries=3
    )
    _assert_outcome(calls, result, ok=False, retries=0)


def _blocked_payload(name, reason):
    payload = _payload(name, "")
    if name == "chat":
        choice = payload["choices"][0]
        if reason == "refusal":
            choice["message"]["refusal"] = "I cannot help with that request."
        elif reason == "tool-call-payload":
            choice["message"]["tool_calls"] = [
                {"id": "call_1", "type": "function", "function": {"name": "search"}}
            ]
        else:
            choice["finish_reason"] = reason
    elif reason == "refusal":
        payload["output"][0]["content"] = [
            {"type": "refusal", "refusal": "I cannot help with that request."}
        ]
    elif reason in {"tool_calls", "tool-call-payload"}:
        payload["output"] = [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "search",
                "arguments": "{}",
            }
        ]
    else:
        payload["output"] = [
            {"type": "message", "content": [{"type": "output_text", "text": ""}]}
        ]
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": reason}
    return payload


@pytest.mark.parametrize(
    "reason",
    [
        "refusal",
        "prohibited_content",
        "content_filter",
        "tool_calls",
        "tool-call-payload",
    ],
)
def test_explicit_nontransient_empty_response_is_not_retried(
    monkeypatch, adapter, reason
):
    name = adapter[0]
    calls, result = _run(
        monkeypatch,
        adapter,
        [_json_response(_blocked_payload(name, reason)), _success(name)],
        max_retries=3,
    )
    _assert_outcome(calls, result, ok=False, retries=0)


@pytest.mark.parametrize("status", [400, 401, 403])
@pytest.mark.parametrize("retry_http", [True, False])
def test_permanent_http_errors_never_retry_even_if_allowlisted(
    monkeypatch, adapter, status, retry_http
):
    calls, result = _run(
        monkeypatch,
        adapter,
        [_Response("Rejected", status=status), _success(adapter[0])],
        max_retries=3,
        retry_http=retry_http,
        retryable_status_codes={status, 500},
    )
    _assert_outcome(calls, result, ok=False, retries=0)
    assert result["status"] == status


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.parametrize("retry_http", [True, False])
def test_http_retry_gate(monkeypatch, adapter, status, retry_http):
    calls, result = _run(
        monkeypatch,
        adapter,
        [_Response("Temporary failure", status=status), _success(adapter[0])],
        max_retries=1,
        retry_http=retry_http,
    )
    _assert_outcome(calls, result, ok=retry_http, retries=int(retry_http))


def test_http_status_outside_allowlist_is_not_retried(monkeypatch, adapter):
    calls, result = _run(
        monkeypatch,
        adapter,
        [_Response("Temporary failure", status=500), _success(adapter[0])],
        max_retries=3,
        retryable_status_codes={429},
    )
    _assert_outcome(calls, result, ok=False, retries=0)


@pytest.mark.parametrize("failure", [aiohttp.ClientConnectionError, TimeoutError])
@pytest.mark.parametrize("retry_http", [True, False])
@pytest.mark.parametrize("max_retries", [0, 1])
def test_network_retry_gate(monkeypatch, adapter, failure, retry_http, max_retries):
    should_retry = retry_http and max_retries > 0
    calls, result = _run(
        monkeypatch,
        adapter,
        [failure("Temporary failure"), _success(adapter[0])],
        max_retries=max_retries,
        retry_http=retry_http,
    )
    _assert_outcome(calls, result, ok=should_retry, retries=int(should_retry))


@pytest.mark.parametrize("max_retries", [0, 1, 2])
@pytest.mark.parametrize("kind", ["empty-body", "empty-final-text"])
@pytest.mark.parametrize("empty_first", [False, True])
def test_http_and_empty_response_consume_the_same_budget(
    monkeypatch, adapter, max_retries, kind, empty_first
):
    name = adapter[0]
    failures = [_Response("Temporary failure", status=500), _empty(name, kind)]
    if empty_first:
        failures.reverse()
    calls, result = _run(
        monkeypatch, adapter, [*failures, _success(name)], max_retries=max_retries
    )
    _assert_outcome(calls, result, ok=max_retries == 2, retries=max_retries)


def _sse_response(text, **choice_fields):
    choice = {"delta": {"content": text}, **choice_fields}
    return _Response(
        "data: " + json.dumps({"choices": [choice]}) + "\n\ndata: [DONE]\n\n",
        content_type="text/event-stream",
    )


@pytest.mark.parametrize("text", ["", " \t\n "])
@pytest.mark.parametrize("max_retries", [0, 1])
@pytest.mark.parametrize("retry_http", [True, False])
def test_chat_empty_sse_uses_the_shared_budget(
    monkeypatch, text, max_retries, retry_http
):
    chat = load("api.grok_chat")
    calls, result = _run(
        monkeypatch,
        ("chat", chat, chat.grok_search),
        [_sse_response(text), _sse_response('{"content":"Answer","sources":[]}')],
        max_retries=max_retries,
        retry_http=retry_http,
        stream=True,
    )
    _assert_outcome(calls, result, ok=max_retries == 1, retries=max_retries)
    assert all(kwargs["json"]["stream"] is True for _, kwargs in calls)


@pytest.mark.parametrize(
    "reason", ["prohibited_content", "content_filter", "tool_calls"]
)
def test_chat_blocked_sse_is_not_retried(monkeypatch, reason):
    chat = load("api.grok_chat")
    calls, result = _run(
        monkeypatch,
        ("chat", chat, chat.grok_search),
        [_sse_response("", finish_reason=reason), _success("chat")],
        max_retries=3,
        stream=True,
    )
    _assert_outcome(calls, result, ok=False, retries=0)
