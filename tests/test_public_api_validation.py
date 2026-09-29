"""SDK 搜索参数校验无需完整 AstrBot 运行时即可验证。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import load

public_api = load("public_api")


@pytest.fixture
def search_service():
    config = {"base_url": "https://grok.example", "api_key": "sdk-fixture-key"}
    result = {"ok": True, "content": "Answer", "sources": []}
    backend = AsyncMock(return_value=result)
    plugin = SimpleNamespace(_cfg=config.get, _do_search=backend)
    service = public_api.GrokSearchService(plugin)
    service.mark_initialized()
    return service, backend


@pytest.mark.parametrize(
    "prompt",
    [{}, {"prompt": "bad"}, 0, 123, False, True, 1.5, [], (), b"prompt"],
    ids=[
        "empty-dict",
        "dict",
        "zero",
        "int",
        "false",
        "true",
        "float",
        "list",
        "tuple",
        "bytes",
    ],
)
def test_search_rejects_non_string_system_prompt_before_dispatch(
    search_service, prompt
):
    service, backend = search_service
    with pytest.raises(public_api.PluginServiceError) as exc_info:
        asyncio.run(service.search("q", system_prompt=prompt))
    assert exc_info.value.code == "invalid_request"
    backend.assert_not_called()


@pytest.mark.parametrize("prompt", [None, "", "   ", "自定义 SDK 提示词"])
def test_search_accepts_none_or_string_system_prompt(search_service, prompt):
    service, backend = search_service
    result = asyncio.run(service.search("q", system_prompt=prompt))
    assert result is backend.return_value
    backend.assert_awaited_once_with(
        "q",
        system_prompt=prompt,
        use_retry=False,
        images=None,
        search_depth="basic",
        max_results=7,
        topic="general",
        days=0,
        time_range="",
        start_date="",
        end_date="",
    )
