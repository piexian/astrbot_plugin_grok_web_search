"""SDK deadlines, diagnostic safety, and executable documentation."""

import ast
import asyncio
import re
from types import SimpleNamespace

import pytest
from conftest import ROOT, load

public_api = load("public_api")


def _service(**overrides):
    config = {"base_url": "https://grok.example", "api_key": "fixture-key"}
    config.update(overrides)
    service = public_api.GrokSearchService(SimpleNamespace(_cfg=config.get))
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
