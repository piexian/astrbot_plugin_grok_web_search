"""SDK 反向搜图凭据的就绪状态与调用准入。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import load

public_api = load("public_api")


@pytest.mark.parametrize("provider", ["serpapi", "saucenao"])
@pytest.mark.parametrize("has_grok_credentials", [False, True])
@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        " \t\n",
        0,
        123,
        False,
        True,
        1.5,
        {},
        {"bad": "key"},
        [],
        ["bad"],
        (),
        b"key",
    ],
    ids=[
        "none",
        "empty",
        "whitespace",
        "zero",
        "integer",
        "false",
        "true",
        "float",
        "empty-dict",
        "dict",
        "empty-list",
        "list",
        "tuple",
        "bytes",
    ],
)
def test_invalid_image_credentials_reject_selected_backend(
    provider, has_grok_credentials, key
):
    config = {f"{provider}_api_key": key}
    if has_grok_credentials:
        config.update(base_url="https://example.com/v1", api_key="grok-key")
    backend = AsyncMock(return_value={"ok": True})
    service = public_api.GrokSearchService(
        SimpleNamespace(_cfg=config.get, _run_reverse_image_search=backend)
    )
    service.mark_initialized()

    status = service.get_status()
    assert status["image_search_ready"] is False
    assert status["ready"] is has_grok_credentials

    with pytest.raises(public_api.PluginServiceError) as exc_info:
        asyncio.run(
            service.reverse_image_search(["fixture-image"], **{f"use_{provider}": True})
        )
    assert exc_info.value.code == "not_ready"
    backend.assert_not_called()


@pytest.mark.parametrize("provider", ["serpapi", "saucenao"])
@pytest.mark.parametrize("key", ["image-key", " \timage-key\n"])
def test_valid_image_credentials_admit_selected_backend(provider, key):
    config = {f"{provider}_api_key": key}
    result = {"ok": True, "evidence_text": "fixture evidence"}
    backend = AsyncMock(return_value=result)
    service = public_api.GrokSearchService(
        SimpleNamespace(_cfg=config.get, _run_reverse_image_search=backend)
    )
    service.mark_initialized()

    status = service.get_status()
    assert status["image_search_ready"] is True
    assert status["ready"] is True

    actual = asyncio.run(
        service.reverse_image_search(["fixture-image"], **{f"use_{provider}": True})
    )
    assert actual == result
    backend.assert_called_once_with(
        ["fixture-image"], provider == "serpapi", provider == "saucenao"
    )
    backend.assert_awaited_once()
