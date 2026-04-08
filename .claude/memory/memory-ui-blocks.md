# UI Blocks (Tool Forms in Chat)

## Summary
Tools can push interactive forms into the chat UI by returning a `ToolOutput` instead of a plain string. Forms render inline and submit back through a dedicated endpoint.

## Details

### Interface (`src/gilbert/interfaces/ui.py`)
- **`ToolOutput(text, ui_blocks)`** — extended return type for `execute_tool()`. `text` goes to the AI conversation, `ui_blocks` are collected for the frontend. Backward compatible — tools returning `str` are unchanged.
- **`UIBlock(block_type, block_id, title, elements, submit_label, tool_name)`** — a form definition. `block_id` is auto-generated if empty. `tool_name` is auto-tagged from the tool call if empty.
- **`UIElement(type, name, label, ...)`** — a single form element. Types: `text`, `textarea`, `select`, `radio`, `checkbox`, `range`, `buttons`, `label`, `separator`.
- **`UIOption(value, label, selected)`** — option for select/radio/checkbox/buttons elements.
- All types have `to_dict()` / `from_dict()` for JSON serialization.

### AI Service Integration
- `_execute_tool_calls()` returns `tuple[list[ToolResult], list[UIBlock]]` — detects `ToolOutput` instances and separates text from blocks.
- `chat()` returns `tuple[str, str, list[dict]]` — 3rd element is serialized UI blocks (possibly empty).
- UI blocks are persisted in the conversation document under `"ui_blocks"` with `response_index`, `submitted`, and `submission` fields.
- All callers of `chat()` updated to handle the 3-tuple (greeting, roast, inbox_ai_chat, slack discard the blocks).

### Chat Route (`src/gilbert/web/routes/chat.py`)
- `chat_send` and `get_conversation` return `ui_blocks` in the response.
- **`POST /chat/form-submit`** — receives `{conversation_id, block_id, values}`, marks the block as submitted in storage, converts values to a text message like `[Form submitted: Title]\n- field: value`, sends through normal `chat()` flow.

### Frontend (`src/gilbert/web/templates/chat.html`)
- `renderUIBlock(block)` renders form elements as interactive HTML using DOM APIs (not innerHTML).
- `gatherFormValues(container)` collects values from all input types.
- `doFormSubmit()` posts to `/chat/form-submit`, disables the form, renders the AI response.
- `loadConversation()` restores stored forms at correct positions using `response_index`, shows submitted forms as disabled.
- `buttons` type elements submit immediately on click with the clicked button's value.

### CSS
Form styles are in `style.css` under the "Chat forms" section. Key classes: `.chat-form-block`, `.chat-form-field`, `.chat-form-submit`, `.chat-form-btn`, `.chat-form-separator`, `.submitted`.

### Usage Example
```python
from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement, UIOption

async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
    return ToolOutput(
        text="Here's a configuration form.",
        ui_blocks=[UIBlock(
            title="Settings",
            elements=[
                UIElement(type="select", name="mode", label="Mode", options=[
                    UIOption("auto", "Automatic"),
                    UIOption("manual", "Manual"),
                ]),
                UIElement(type="range", name="level", label="Level", min_val=0, max_val=100, default=50),
            ],
            submit_label="Apply",
        )],
    )
```

## Related
- [AI Service](memory-ai-service.md) — agentic loop collects UI blocks
- `tests/unit/test_ui_blocks.py` — 17 tests covering serialization, ToolOutput handling, and AI service integration
