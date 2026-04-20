"""Tests for UI blocks system — ToolOutput handling, serialization, and AI service integration."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement, UIOption

# --- Serialization round-trip ---


class TestUIElementSerialization:
    def test_text_element_round_trip(self) -> None:
        el = UIElement(
            type="text", name="email", label="Email", placeholder="you@example.com", required=True
        )
        d = el.to_dict()
        assert d == {
            "type": "text",
            "name": "email",
            "label": "Email",
            "placeholder": "you@example.com",
            "required": True,
        }
        restored = UIElement.from_dict(d)
        assert restored.type == "text"
        assert restored.name == "email"
        assert restored.required is True

    def test_select_element_with_options(self) -> None:
        el = UIElement(
            type="select",
            name="room",
            label="Room",
            options=[
                UIOption("living", "Living Room"),
                UIOption("kitchen", "Kitchen", selected=True),
            ],
        )
        d = el.to_dict()
        assert len(d["options"]) == 2
        assert d["options"][1] == {"value": "kitchen", "label": "Kitchen", "selected": True}
        # selected=False should be omitted
        assert "selected" not in d["options"][0]

        restored = UIElement.from_dict(d)
        assert len(restored.options) == 2
        assert restored.options[1].selected is True

    def test_range_element(self) -> None:
        el = UIElement(
            type="range", name="vol", label="Volume", min_val=0, max_val=100, step=5, default=50
        )
        d = el.to_dict()
        assert d["min"] == 0
        assert d["max"] == 100
        assert d["step"] == 5
        assert d["default"] == 50

    def test_textarea_includes_rows(self) -> None:
        el = UIElement(type="textarea", name="notes", rows=6)
        d = el.to_dict()
        assert d["rows"] == 6

    def test_label_element_minimal(self) -> None:
        el = UIElement(type="label", label="Instructions")
        d = el.to_dict()
        assert d == {"type": "label", "label": "Instructions"}

    def test_separator_element_minimal(self) -> None:
        el = UIElement(type="separator")
        d = el.to_dict()
        assert d == {"type": "separator"}

    def test_image_element_round_trip(self) -> None:
        el = UIElement(
            type="image",
            name="poster",
            url="https://img.example/poster.jpg",
            label="Inception poster",
            max_width=96,
        )
        d = el.to_dict()
        assert d["type"] == "image"
        assert d["url"] == "https://img.example/poster.jpg"
        assert d["max_width"] == 96
        assert d["label"] == "Inception poster"

        restored = UIElement.from_dict(d)
        assert restored.type == "image"
        assert restored.url == "https://img.example/poster.jpg"
        assert restored.max_width == 96
        assert restored.label == "Inception poster"

    def test_image_element_without_url_and_max_width_omits_keys(self) -> None:
        """Unset url / max_width should not appear in the serialized dict.

        Keeps on-wire payloads small and matches the other optional fields'
        behavior.
        """
        el = UIElement(type="label", label="just a label")
        d = el.to_dict()
        assert "url" not in d
        assert "max_width" not in d


class TestUIBlockSerialization:
    def test_form_block_round_trip(self) -> None:
        block = UIBlock(
            block_id="test-123",
            title="Settings",
            elements=[
                UIElement(type="text", name="name", label="Name"),
                UIElement(type="checkbox", name="notify", label="Notify me"),
            ],
            submit_label="Save",
            tool_name="config_tool",
        )
        d = block.to_dict()
        assert d["block_type"] == "form"
        assert d["block_id"] == "test-123"
        assert d["title"] == "Settings"
        assert d["submit_label"] == "Save"
        assert d["tool_name"] == "config_tool"
        assert len(d["elements"]) == 2

        restored = UIBlock.from_dict(d)
        assert restored.block_id == "test-123"
        assert restored.title == "Settings"
        assert len(restored.elements) == 2

    def test_auto_generates_block_id(self) -> None:
        block = UIBlock(title="Test")
        d = block.to_dict()
        assert d["block_id"]  # Should be a non-empty UUID
        assert len(d["block_id"]) == 36  # UUID format

    def test_empty_block_defaults(self) -> None:
        block = UIBlock()
        d = block.to_dict()
        assert d["block_type"] == "form"
        assert d["submit_label"] == "Submit"
        assert d["elements"] == []


# --- ToolOutput ---


class TestToolOutput:
    def test_text_only(self) -> None:
        out = ToolOutput(text="Done.")
        assert out.text == "Done."
        assert out.ui_blocks == []

    def test_with_blocks(self) -> None:
        block = UIBlock(
            title="Pick one",
            elements=[
                UIElement(
                    type="buttons",
                    name="choice",
                    options=[
                        UIOption("a", "Option A"),
                        UIOption("b", "Option B"),
                    ],
                ),
            ],
        )
        out = ToolOutput(text="Here are your options.", ui_blocks=[block])
        assert len(out.ui_blocks) == 1
        assert out.ui_blocks[0].title == "Pick one"


# --- AI service integration ---


class TestToolOutputInAgenticLoop:
    """Test that _execute_tool_calls correctly handles ToolOutput returns."""

    @pytest.fixture
    def ai_service(self) -> MagicMock:
        """Minimal mock of AIService with _execute_tool_calls accessible."""
        from gilbert.core.services.ai import AIService

        svc = MagicMock(spec=AIService)
        # Use the real methods
        svc._execute_tool_calls = AIService._execute_tool_calls.__get__(svc, AIService)
        svc._run_one_tool = AIService._run_one_tool.__get__(svc, AIService)
        svc._group_is_parallel = AIService._group_is_parallel
        svc._publish_event = AIService._publish_event.__get__(svc, AIService)
        svc._sanitize_tool_args = AIService._sanitize_tool_args
        svc._acl_svc = None
        svc._resolver = None
        svc._storage = None
        return svc

    async def test_plain_string_result(self, ai_service: MagicMock) -> None:
        """Backward compat: plain str returns work as before."""
        from gilbert.interfaces.tools import ToolCall, ToolDefinition

        provider = MagicMock()
        provider.execute_tool = AsyncMock(return_value="plain text result")
        tool_def = ToolDefinition(name="test_tool", description="test", parameters=[])

        tc = ToolCall(tool_call_id="call_1", tool_name="test_tool", arguments={})
        tools_by_name = {"test_tool": (provider, tool_def)}

        results, ui_blocks = await ai_service._execute_tool_calls(
            [tc],
            tools_by_name,
            user_ctx=None,
            profile=None,
        )

        assert len(results) == 1
        assert results[0].content == "plain text result"
        assert results[0].is_error is False
        assert ui_blocks == []

    async def test_tool_output_extracts_blocks(self, ai_service: MagicMock) -> None:
        """ToolOutput returns have text in result and blocks collected separately."""
        from gilbert.interfaces.tools import ToolCall, ToolDefinition

        block = UIBlock(block_id="blk-1", title="Test Form", tool_name="my_tool")
        provider = MagicMock()
        provider.execute_tool = AsyncMock(
            return_value=ToolOutput(
                text="Form shown.",
                ui_blocks=[block],
            )
        )
        tool_def = ToolDefinition(name="my_tool", description="test", parameters=[])

        tc = ToolCall(tool_call_id="call_2", tool_name="my_tool", arguments={})
        tools_by_name = {"my_tool": (provider, tool_def)}

        results, ui_blocks = await ai_service._execute_tool_calls(
            [tc],
            tools_by_name,
            user_ctx=None,
            profile=None,
        )

        assert len(results) == 1
        assert results[0].content == "Form shown."
        assert len(ui_blocks) == 1
        assert ui_blocks[0].title == "Test Form"

    async def test_auto_assigns_block_id(self, ai_service: MagicMock) -> None:
        """Blocks without block_id get one auto-assigned."""
        from gilbert.interfaces.tools import ToolCall, ToolDefinition

        block = UIBlock(title="No ID")  # block_id is ""
        provider = MagicMock()
        provider.execute_tool = AsyncMock(
            return_value=ToolOutput(
                text="ok",
                ui_blocks=[block],
            )
        )
        tool_def = ToolDefinition(name="t", description="test", parameters=[])

        tc = ToolCall(tool_call_id="c1", tool_name="t", arguments={})
        results, ui_blocks = await ai_service._execute_tool_calls(
            [tc],
            {"t": (provider, tool_def)},
            user_ctx=None,
            profile=None,
        )

        assert len(ui_blocks) == 1
        assert ui_blocks[0].block_id  # Should be non-empty
        assert ui_blocks[0].block_id != ""

    async def test_auto_assigns_tool_name(self, ai_service: MagicMock) -> None:
        """Blocks without tool_name get it from the tool call."""
        from gilbert.interfaces.tools import ToolCall, ToolDefinition

        block = UIBlock(title="Form")  # tool_name is ""
        provider = MagicMock()
        provider.execute_tool = AsyncMock(
            return_value=ToolOutput(
                text="ok",
                ui_blocks=[block],
            )
        )
        tool_def = ToolDefinition(name="config_tool", description="test", parameters=[])

        tc = ToolCall(tool_call_id="c1", tool_name="config_tool", arguments={})
        results, ui_blocks = await ai_service._execute_tool_calls(
            [tc],
            {"config_tool": (provider, tool_def)},
            user_ctx=None,
            profile=None,
        )

        assert ui_blocks[0].tool_name == "config_tool"

    async def test_error_returns_no_blocks(self, ai_service: MagicMock) -> None:
        """Tool exceptions produce error result and no UI blocks."""
        from gilbert.interfaces.tools import ToolCall, ToolDefinition

        provider = MagicMock()
        provider.execute_tool = AsyncMock(side_effect=RuntimeError("boom"))
        tool_def = ToolDefinition(name="t", description="test", parameters=[])

        tc = ToolCall(tool_call_id="c1", tool_name="t", arguments={})
        results, ui_blocks = await ai_service._execute_tool_calls(
            [tc],
            {"t": (provider, tool_def)},
            user_ctx=None,
            profile=None,
        )

        assert len(results) == 1
        assert results[0].is_error is True
        assert ui_blocks == []

    async def test_mixed_tools_accumulate_blocks(self, ai_service: MagicMock) -> None:
        """Multiple tool calls in one batch: blocks from all are collected."""
        from gilbert.interfaces.tools import ToolCall, ToolDefinition

        provider1 = MagicMock()
        provider1.execute_tool = AsyncMock(return_value="plain")
        provider2 = MagicMock()
        provider2.execute_tool = AsyncMock(
            return_value=ToolOutput(
                text="form here",
                ui_blocks=[UIBlock(block_id="b1", title="F1")],
            )
        )
        provider3 = MagicMock()
        provider3.execute_tool = AsyncMock(
            return_value=ToolOutput(
                text="another",
                ui_blocks=[UIBlock(block_id="b2", title="F2")],
            )
        )

        def td(name: str) -> ToolDefinition:
            return ToolDefinition(name=name, description="test", parameters=[])

        calls = [
            ToolCall(tool_call_id="c1", tool_name="a", arguments={}),
            ToolCall(tool_call_id="c2", tool_name="b", arguments={}),
            ToolCall(tool_call_id="c3", tool_name="c", arguments={}),
        ]
        tools = {
            "a": (provider1, td("a")),
            "b": (provider2, td("b")),
            "c": (provider3, td("c")),
        }

        results, ui_blocks = await ai_service._execute_tool_calls(
            calls,
            tools,
            user_ctx=None,
            profile=None,
        )

        assert len(results) == 3
        assert len(ui_blocks) == 2
        assert {b.block_id for b in ui_blocks} == {"b1", "b2"}
