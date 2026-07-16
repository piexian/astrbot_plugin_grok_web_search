"""
Tests for ContextWrapper compatibility fix.

When AstrBot's LLM Tool path invokes a plugin, the framework passes a
``ContextWrapper`` (which exposes ``.messages`` as a property) instead of a
raw ``AstrMessageEvent`` (which exposes ``.get_messages()`` as a method).
This test module verifies that both ``_extract_content_from_event`` and
``_message_has_quoted`` degrade gracefully when ``.get_messages()`` is
missing.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Package discovery
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_PKG_NAME = _PLUGIN_ROOT.name  # "grok_plugin_fix"

# The parent of the plugin root must be on sys.path so we can import it as a
# package (the root already has an __init__.py).
_PARENT = str(_PLUGIN_ROOT.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# ---------------------------------------------------------------------------
# Mock astrbot & astrbot.core dependencies *before* importing the plugin.
# This avoids needing the full AstrBot runtime installed.
# ---------------------------------------------------------------------------

# --- types for AstrMessageEvent / message components -------------------------

_mock_star_base = type("Star", (), {"__init__": lambda self, context, config=None: None})

_Forward = type("Forward", (), {"__init__": lambda self, *a, **kw: None})
_Image = type("Image", (), {"__init__": lambda self, *a, **kw: None})
_Node = type("Node", (), {"__init__": lambda self, *a, **kw: None})
_Nodes = type("Nodes", (), {"__init__": lambda self, *a, **kw: None})
_Plain = type("Plain", (), {"__init__": lambda self, *a, **kw: None})
_Reply = type("Reply", (), {"__init__": lambda self, *a, **kw: None})

# --- mock module trees -------------------------------------------------------

def _mock(name, **attrs):
    m = MagicMock(**attrs)
    sys.modules[name] = m
    return m


# astrbot.api.*
_astrbot = _mock("astrbot")
_astrbot.api = _mock("astrbot.api")
_astrbot.api.event = _mock("astrbot.api.event", AstrMessageEvent=MagicMock, MessageChain=MagicMock, filter=MagicMock())
_astrbot.api.star = _mock("astrbot.api.star", Context=MagicMock, Star=_mock_star_base)
_astrbot.logger = MagicMock()

# astrbot.core.*
_mock("astrbot.core.message.components",
      Forward=_Forward, Image=_Image, Node=_Node, Nodes=_Nodes, Plain=_Plain, Reply=_Reply)
_mock("astrbot.core.star.filter.command", GreedyStr=MagicMock())
_mock("astrbot.core.utils.io", download_image_by_url=MagicMock(), file_to_base64=MagicMock())
_mock("astrbot.core.utils.quoted_message.chain_parser",
      _extract_image_refs_from_component_chain=MagicMock(return_value=[]),
      _extract_text_from_component_chain=MagicMock(return_value=""))
_mock("astrbot.core.utils.quoted_message_parser")
_mock("astrbot.core.provider.register")

# Plugin sub-packages (relative imports from main resolve to <pkg>.api etc.)
_mock(f"{_PKG_NAME}.api")
_mock(f"{_PKG_NAME}.api.grok_chat", grok_fetch=MagicMock(), grok_search=MagicMock())
_mock(f"{_PKG_NAME}.api.grok_responses", grok_responses_search=MagicMock())

_mock(f"{_PKG_NAME}.tool")
_mock(f"{_PKG_NAME}.tool.card_render",
      init_fonts=MagicMock(), render_search_card=MagicMock(), set_logger=MagicMock())
_mock(f"{_PKG_NAME}.tool.font_loader")
_tool_mod = _mock(f"{_PKG_NAME}.tool.tool",
      DEFAULT_JSON_SYSTEM_PROMPT="",
      DEFAULT_MODEL="grok-3",
      build_headers=MagicMock(return_value={}),
      build_search_time_constraints=MagicMock(return_value={}),
      normalize_api_key=MagicMock(return_value=""),
      normalize_base_url=MagicMock(return_value=""),
      normalize_search_options=MagicMock(return_value={}),
      parse_json_config=MagicMock(return_value={}),
      resolve_mode_model=MagicMock(return_value="grok-3"),
      resolve_reasoning_params=MagicMock(return_value={}),
      resolve_search_mode=MagicMock(return_value="quick"),
      resolve_system_prompt=MagicMock(return_value=""),
      safe_number=MagicMock(return_value=0))

# ---------------------------------------------------------------------------
# Now import the plugin module safely.
# ---------------------------------------------------------------------------

import importlib

_main = importlib.import_module(f"{_PKG_NAME}.main")
GrokSearchPlugin = _main.GrokSearchPlugin

# Convenience aliases for test assertions.
Plain = _Plain
Reply = _Reply
Forward = _Forward
Node = _Node
Nodes = _Nodes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeStar:
    """Minimal base that mirrors Star's constructor signature."""

    def __init__(self, context=None, config=None):
        pass


class FakeEvent:
    """An event-like object whose ``get_messages()`` can be controlled."""

    def __init__(self, messages=None, *, raise_attribute_error=False):
        self._messages = messages or []
        self._raise = raise_attribute_error

    def get_messages(self):
        if self._raise:
            raise AttributeError(
                "type object 'ContextWrapper' has no attribute 'get_messages'"
            )
        return list(self._messages)


class FakeContextWrapperEvent(FakeEvent):
    """Simulates a ContextWrapper: has ``.messages`` but NOT ``.get_messages()``."""

    def __init__(self, messages=None):
        super().__init__(messages, raise_attribute_error=True)
        self.messages = messages or []


# ---------------------------------------------------------------------------
# _message_has_quoted  (static method)
# ---------------------------------------------------------------------------

class TestMessageHasQuoted:
    """Tests for ``GrokSearchPlugin._message_has_quoted``."""

    def test_empty_chain_returns_false(self):
        event = FakeEvent(messages=[])
        assert GrokSearchPlugin._message_has_quoted(event) is False

    def test_plain_only_returns_false(self):
        event = FakeEvent(messages=[Plain("hello")])
        assert GrokSearchPlugin._message_has_quoted(event) is False

    def test_reply_component_returns_true(self):
        event = FakeEvent(messages=[Reply("msg_id")])
        assert GrokSearchPlugin._message_has_quoted(event) is True

    def test_forward_component_returns_true(self):
        event = FakeEvent(messages=[Forward()])
        assert GrokSearchPlugin._message_has_quoted(event) is True

    def test_node_component_returns_true(self):
        event = FakeEvent(messages=[Node()])
        assert GrokSearchPlugin._message_has_quoted(event) is True

    def test_nodes_component_returns_true(self):
        event = FakeEvent(messages=[Nodes()])
        assert GrokSearchPlugin._message_has_quoted(event) is True

    def test_mixed_chain_with_reply_returns_true(self):
        event = FakeEvent(messages=[Plain("hello"), Reply("id")])
        assert GrokSearchPlugin._message_has_quoted(event) is True

    # -- ContextWrapper compat ------------------------------------------------

    def test_context_wrapper_with_chain_components_detected(self):
        """ContextWrapper with .messages containing Reply → still works."""
        event = FakeContextWrapperEvent(messages=[Reply("id")])
        assert GrokSearchPlugin._message_has_quoted(event) is True

    def test_context_wrapper_with_non_component_messages_returns_false(self):
        """ContextWrapper with non-chain messages (e.g. LLM history) → False."""
        event = FakeContextWrapperEvent(messages=[object()])
        assert GrokSearchPlugin._message_has_quoted(event) is False


# ---------------------------------------------------------------------------
# _extract_content_from_event  (async instance method)
# ---------------------------------------------------------------------------

class TestExtractContentFromEvent:
    """Tests for ``GrokSearchPlugin._extract_content_from_event``."""

    @pytest.fixture
    def plugin(self):
        """Create a bare GrokSearchPlugin instance bypassing Star.__init__."""
        orig_bases = GrokSearchPlugin.__bases__
        GrokSearchPlugin.__bases__ = (_FakeStar,)
        try:
            inst = GrokSearchPlugin(context=None, config={})
        finally:
            GrokSearchPlugin.__bases__ = orig_bases
        return inst

    # -- ContextWrapper compat ------------------------------------------------

    @pytest.mark.asyncio
    async def test_context_wrapper_falls_back_to_messages(self, plugin):
        """When get_messages() fails, fall back to event.messages."""
        event = FakeContextWrapperEvent()  # empty messages
        text, images = await plugin._extract_content_from_event(event)
        assert text == ""  # mock chain_parser returns ""
        assert images == []

    # -- Normal event path (smoke tests) --------------------------------------

    @pytest.mark.asyncio
    async def test_normal_event_empty_chain(self, plugin):
        """Normal event with empty message chain returns (text, [])."""
        event = FakeEvent(messages=[])
        text, images = await plugin._extract_content_from_event(event)
        assert isinstance(text, (str, type(None)))
        assert images == []
