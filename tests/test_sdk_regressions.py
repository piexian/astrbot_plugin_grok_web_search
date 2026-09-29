"""SDK deadlines, diagnostic safety, and executable documentation."""

import ast
import asyncio
import re
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import ROOT, load

public_api = load("public_api")
fetch_service = load("tool.fetch_service")


def _service(**overrides):
    config = {
        "base_url": "https://grok.example",
        "api_key": "fixture-key",
        "enable_fetch": True,
    }
    config.update(overrides)
    service = public_api.GrokSearchService(
        SimpleNamespace(
            _cfg=config.get, _do_fetch=partial(fetch_service.execute_fetch, config.get)
        )
    )
    service.mark_initialized()
    return service


@pytest.mark.parametrize("timeout", [0, -1])
def test_wait_ready_returns_existing_ready_snapshot_without_waiting(timeout):
    service = _service()
    assert asyncio.run(service.wait_ready(timeout)) == service.get_status()


@pytest.mark.parametrize("timeout", [0, -1])
@pytest.mark.parametrize("close", ["begin_shutdown", "close"])
def test_closed_state_takes_precedence_over_wait_deadline(timeout, close):
    service = _service()
    getattr(service, close)()
    with pytest.raises(public_api.PluginServiceError) as error:
        asyncio.run(service.wait_ready(timeout))
    assert error.value.code == "service_closed"


@pytest.mark.parametrize("field", ["base_url", "api_key"])
@pytest.mark.parametrize("value", [None, 123, {}])
def test_status_handles_cleared_or_malformed_credentials(field, value):
    service = _service(**{field: value})
    assert service.get_status()["state"] == "unavailable"
    service.close()
    assert service.get_status()["state"] == "closed"


@pytest.mark.parametrize("document", ["README.md", "docs/plugin-api.md"])
def test_sdk_documentation_python_examples_compile(document):
    text = (ROOT / document).read_text(encoding="utf-8")
    if document == "README.md":
        text = text.split("## 插件服务接口（SDK v1）", 1)[1].split("## 项目结构", 1)[0]
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    assert blocks
    for block in blocks:
        compile(block, document, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


@pytest.mark.parametrize(
    "url",
    [
        "http-not-a-url",
        "httpx://host",
        "http nonsense",
        "https://",
        "https:///path",
        "https://[broken",
        "https://exa mple.org",
        "https://host:notport",
        "https://host:70000",
    ],
)
def test_sdk_fetch_rejects_malformed_http_urls_without_requests(monkeypatch, url):
    api = AsyncMock(return_value={"ok": True, "content": "fixture"})
    monkeypatch.setattr(fetch_service, "_load_api", lambda: api)
    result = asyncio.run(_service().fetch(url))
    assert result["ok"] is False and result["error_kind"] == "invalid_url"
    api.assert_not_awaited()


@pytest.mark.parametrize(
    "timeout,expected",
    [
        ("not-a-number", 60.0),
        ([], 60.0),
        ({}, 60.0),
        (None, 60.0),
        (0, 60.0),
        (-1, 60.0),
        ("12.5", 12.5),
    ],
)
def test_sdk_fetch_normalizes_timeout_like_search(monkeypatch, timeout, expected):
    api = AsyncMock(return_value={"ok": True, "content": "fixture"})
    monkeypatch.setattr(fetch_service, "_load_api", lambda: api)
    result = asyncio.run(_service(timeout_seconds=timeout).fetch("https://example.org"))
    assert result["ok"] is True
    assert api.await_args.kwargs["timeout"] == expected
    assert api.await_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/docs",
        "http://localhost:8080/p?q=1",
        "HTTPS://Example.org/a",
    ],
)
def test_sdk_fetch_accepts_complete_http_urls(monkeypatch, url):
    api = AsyncMock(return_value={"ok": True, "content": "fixture"})
    monkeypatch.setattr(fetch_service, "_load_api", lambda: api)
    assert asyncio.run(_service().fetch(url))["ok"] is True
    assert api.await_args.kwargs["url"] == url
    assert api.await_count == 1
