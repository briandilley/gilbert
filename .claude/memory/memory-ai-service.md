# AI Service

## Summary
Central AI service that orchestrates conversations with tool use. Uses the AIBackend ABC + backend registry pattern. Currently uses Anthropic Claude via direct httpx calls (both the non-streaming `generate()` path for tests/simple callers and an SSE streaming `generate_stream()` path). Includes internal helpers for persona and user memory (previously separate services).

## Details

### Architecture Layers
- **`interfaces/attachments.py`** — `FileAttachment` dataclass (moved here so it can be shared between `ai.py` and `tools.py` without an import cycle)
- **`interfaces/tools.py`** — `ToolProvider` protocol (runtime_checkable), `ToolDefinition`, `ToolCall`, `ToolResult` (with `attachments` tuple), `ToolParameterType`
- **`interfaces/ai.py`** — `AIBackend` ABC (with registry pattern + default `generate_stream` fallback), `Message`, `MessageRole`, `AIRequest`, `AIResponse`, `StopReason`, `TokenUsage`, `ChatTurnResult` NamedTuple (6 fields including `rounds`), `StreamEventType` / `StreamEvent`, `AIBackendCapabilities`
- **`interfaces/ui.py`** — `ToolOutput` (text + ui_blocks + attachments)
- **`core/services/ai.py`** — `AIService(Service)` — the orchestrator, plus `_PersonaHelper` and `_MemoryHelper`
- **`std-plugins/anthropic/anthropic_ai.py`** — `AnthropicAI(AIBackend)` — Claude via httpx (lives in the `anthropic` std-plugin, not in core)

### AIService
- **Capabilities:** `ai_chat`, `ai_tools`, `ws_handlers`, `persona`, `user_memory`
- **Requires:** `entity_storage`
- **Optional:** `ai_tools`, `configuration`, `access_control`
- **Main method:** `chat(user_message, conversation_id=None, attachments=None) -> ChatTurnResult`
  - `ChatTurnResult` is a 6-field NamedTuple `(response_text, conversation_id, ui_blocks, tool_usage, attachments, rounds)`. Old 4-tuple callers must use `*_` unpack to absorb the trailing fields; new callers read `.attachments` / `.rounds` explicitly.
- **User attachments:** `FileAttachment` with `kind` ∈ {`image`, `document`, `text`}. Inline mode carries base64 `data` or decoded `text`. Frame parser in `ai.py` (`_parse_frame_attachments`) enforces per-item and total size caps, validates media types, and converts xlsx documents into markdown text attachments at parse time (so binary xlsx never hits storage or the AI). Backends translate attachments to provider blocks — Anthropic emits `image`/`document`/`text` content blocks ordered images → documents → text → user prompt.
- **Assistant attachments (tool-produced):** Tools return `ToolResult(..., attachments=(...))` or `ToolOutput(..., attachments=(...))`. `AIService._execute_tool_calls` collects attachments from each tool result; at the end of the turn they're landed on the final assistant `Message` so they persist with the conversation and ride back on the `chat.message.send.result` WS frame. The preferred form is **workspace-reference** mode: `FileAttachment(kind, name, media_type, workspace_skill, workspace_path, workspace_conv)` with no inline data — the bytes stay on disk under `.gilbert/skill-workspaces/users/<user>/conversations/<conv>/<skill>/<path>` and the frontend fetches them on click via `skills.workspace.download`. `SkillService.attach_workspace_file` is the tool for this; it path-traversal-guards, mime-sniffs, stamps the current `_conversation_id` onto the attachment, and returns it.
- **Agentic loop:** Drives backend.generate_stream() (not generate() directly) — iterating StreamEvents, forwarding TEXT_DELTA to the event bus as `chat.stream.text_delta`, and using MESSAGE_COMPLETE to get the assembled response for tool-call / stop-reason handling. Non-streaming backends inherit a default `generate_stream` that calls `generate()` and yields a single MESSAGE_COMPLETE, so the loop structure is provider-agnostic. Executes tool calls, feeds results back, repeats up to `max_tool_rounds` (default 15).
- **Per-round breakdown (`turn_rounds`):** Alongside the flat `tool_usage` list, the loop builds a structured `turn_rounds` list — one entry per AI round that went through tool execution, shaped `{reasoning, tools: [{tool_call_id, tool_name, arguments, result, is_error}]}`. The reasoning is the assistant text emitted alongside the tool_use blocks for that round. Returned on `ChatTurnResult.rounds` and forwarded on the `chat.message.send.result` frame as the authoritative committed shape for the just-finished turn. The frontend's `TurnBubble` renders one bubble per turn with the rounds nested inside a collapsible thinking card.
- **Max-tokens recovery:** When a round comes back with `stop_reason=max_tokens`:
  - **Text-only** → inject a synthetic "please continue" user message and loop up to `max_continuation_rounds` times (default 2). At persist time, `_collapse_continuations` strips the synthetic user rows and merges adjacent assistant text rows so the saved history reads as one coherent reply.
  - **Partial tool_call** → unrecoverable (truncated JSON input). Strip the broken tool_call, annotate the assistant message with a "raise max_tokens" note, and add a `<max_tokens_truncation>` entry to `tool_usage` so the frontend surfaces it. Log a warning on the `gilbert.ai` logger.
- **Tool argument injection:** Before calling each tool, the loop injects underscore-prefixed args alongside the tool's own params: `_user_id`, `_user_name`, `_user_roles`, `_conversation_id`, `_invocation_source` (`"ai"` from `_execute_tool_calls`, `"slash"` from `_execute_slash_command`), and `_room_members` for shared rooms. `_sanitize_tool_args` strips underscore-prefixed keys before they're shown in the frontend's tool-usage display.
- **Lazy tool discovery:** Tools discovered at each chat() call via `resolver.get_all("ai_tools")`, not during start()
- **Conversation persistence:** Stored in `ai_conversations` collection. `_serialize_message` / `_deserialize_message` round-trip `FileAttachment` including `workspace_skill` / `workspace_path` / `workspace_conv` reference fields.
- **History truncation:** Keeps last `max_history_messages`, never splits tool-call/result pairs
- **Conversation delete:** `_ws_conversation_delete` publishes `chat.conversation.destroyed` with `{conversation_id, owner_id}` after removing the storage row. `SkillService` subscribes to that event and removes the matching `users/<owner>/conversations/<conv>/` workspace tree (see [memory-skill-workspaces](memory-skill-workspaces.md)).

### Streaming Pipeline

- **Interface (`interfaces/ai.py`):** `AIBackendCapabilities` (`streaming`, `attachments_user`), `StreamEventType` (TEXT_DELTA / TOOL_CALL_START / TOOL_CALL_DELTA / TOOL_CALL_END / MESSAGE_COMPLETE), `StreamEvent` dataclass, and `AIBackend.generate_stream()` with a default fallback that wraps `generate()` in a single MESSAGE_COMPLETE event.
- **AnthropicAI:** Overrides `capabilities()` to report `streaming=True, attachments_user=True` and implements `generate_stream()` via httpx SSE (`POST /v1/messages` with `"stream": true`). Parses Anthropic's SSE event sequence in `_dispatch_sse_event`, buffering text chunks and tool_use input JSON by content-block index, and assembles the final `AIResponse` for MESSAGE_COMPLETE after `message_stop`. All Anthropic-specific event names and field layouts are confined to this file.
- **Core loop:** Iterates `generate_stream()` unconditionally. On `TEXT_DELTA`, publishes `chat.stream.text_delta` onto the event bus with `visible_to=[owner_id]` (personal) or `[member_ids...]` (shared). On `MESSAGE_COMPLETE`, captures the assembled `AIResponse` and continues as the old non-streaming loop did. Emits `chat.stream.round_complete` at the end of each round and `chat.stream.turn_complete` at the end of the turn so the frontend can transition between rounds.
- **WS layer:** `chat.stream.*` events are bridged by the existing event-bus → WS dispatcher. `WsConnection.can_see_chat_event` has a dedicated branch for `chat.stream.` that respects `visible_to` (no `shared_conv_ids` membership check, since personal conversations aren't rooms).
- **Frontend:** `ChatPage` keeps a list of `ChatTurn` objects; on send, it appends a `streaming: true` placeholder turn at the bottom. Stream event handlers mutate that turn's rounds in place: `chat.stream.text_delta` appends to the current round's reasoning, `chat.tool.started` adds a running tool entry, `chat.tool.completed` flips it to done. `chat.stream.round_complete` sets a `nextRoundPendingRef` ref so the next text_delta opens a fresh round entry instead of concatenating onto the previous one — this is the explicit round-boundary signal that replaces an earlier broken heuristic that tried to infer round boundaries from "does the last round have tools attached." When the `chat.message.send` RPC resolves, the placeholder is replaced by the authoritative committed turn from the server's `rounds` field.
- **Frontend timeout/keepalive:** `useWebSocket.tsx` has a 600s `LONG_TIMEOUT` for chat-send RPCs (was 120s, raised because long agentic turns with code-gen routinely exceeded it). Any `chat.stream.*` or `chat.tool.*` event for an in-flight RPC's conversation resets the deadline — the timeout only fires when the server has actually been silent for the full budget.

### Frontend Turn UI

- **`TurnBubble.tsx`** renders one bubble per `ChatTurn`. Layout: user message at top (right-aligned, primary color), then a collapsible **thinking card** showing per-round reasoning + tool calls, then the **final answer** at the bottom (markdown content + downloadable attachments).
- **Thinking card** is collapsed by default whether the turn is live or completed. The collapsed header is a 2-line summary that updates live: top line shows status icon (spinner if live, wrench if done) + most recent tool name + counter (`N rounds · M tools`); second line shows the most recent fragment of reasoning text, truncated to ~80 chars. The header pulses while the turn has no final answer yet, goes static once the answer commits. Click to expand and see the full per-round breakdown.
- **`MessageList.tsx`** maps `turns: ChatTurn[]` to `TurnBubble`s. UI block anchoring is by visible-assistant index across turns (turns with no `final_content` don't consume an index).
- **No more `ThinkingPanel.tsx` / `MessageBubble.tsx`** — both were deleted. Tool activity now renders inline in each turn's thinking card; the user-message side of a turn is rendered directly inside `TurnBubble`.

### Internal Helpers (merged services)
- **`_PersonaHelper`** — manages AI persona text in `persona` collection. Exposes tools: `get_persona`, `update_persona`, `reset_persona`
- **`_MemoryHelper`** — per-user persistent memories in `user_memories` collection. Exposes tool: `memory` (actions: remember, recall, update, forget, list). The tool handler (`_tool_memory_action`) reads caller identity from the injected `_user_id` argument — the WS chat path does not set the `get_current_user` contextvar before dispatching tool calls.

### History Replay (`_ws_history_load`)

`chat.history.load.result` returns `turns: list[ChatTurn]`, NOT a flat `messages` list. `_group_persisted_messages_into_turns` walks the persisted message rows and rebuilds the same turn structure the live path emits:

- A `user` row opens a new turn. Any in-progress turn is closed first.
- An `assistant` row with `tool_calls` opens a new round inside the current turn (with the row's `content` as that round's reasoning).
- A `tool_result` row pairs results back to the current round's tools by `tool_call_id`.
- An `assistant` row WITHOUT `tool_calls` is the turn's final answer; closes the turn.
- Slash-command rows (which carry tool_calls + tool_results + content all on one row) collapse into a single round + final answer in one go.
- Turns that never reached a final assistant text (max_tool_rounds, error, etc.) get `incomplete: true` so the UI renders an indicator.

### ToolProvider Protocol
Any service declaring `ai_tools` capability that implements `tool_provider_name`, `get_tools()`, and `execute_tool()` is auto-discovered. `execute_tool` may return a plain `str` (backward compat), a `ToolOutput` (text + ui_blocks + attachments), or a `ToolResult` (full control, including attachments and is_error flag).

### AnthropicAI Backend
- Direct HTTP via httpx.AsyncClient (no anthropic SDK dependency)
- Backend-specific params: `model`, `max_tokens` (default **16384**, up from 4096), `temperature` — declared via `backend_config_params()`
- API key is a backend param (`settings.api_key`, marked sensitive)
- Streams via `generate_stream` + SSE; non-streaming `generate` path still exists for `test_connection` and simple callers
- **Dangling-tool_use heal:** `_build_messages` post-processes the request body via `_heal_dangling_tool_uses` so any historical conversation with an unpaired `tool_use` block (left over from a pre-Stream-1 max_tokens cut-off) gets a synthetic `tool_result` injected before being sent to Anthropic. Without the heal, those rows blow up Anthropic's strict tool_use/tool_result pairing requirement.

### Configuration
AIService implements `Configurable` with `config_category = "Intelligence"`. Params:
- `max_history_messages` — conversation history window (default 50)
- `max_tool_rounds` — max agentic loop iterations (default 15)
- `max_continuation_rounds` — max "please continue" recoveries after a max_tokens cutoff per turn (default 2)
- `backend` — backend provider name (restart required)
- `default_persona` — default persona text (multiline)
- `memory_enabled` — whether AI memory system is enabled (restart required)

Backend params are merged under `settings.*` prefix with `backend_param=True`.

## Related
- [Service System](memory-service-system.md)
- [Storage Backend](memory-storage-backend.md)
- [Configuration Service](memory-configuration-service.md)
- [Event System](memory-event-system.md)
- [Skill Workspaces & Activation Gate](memory-skill-workspaces.md)
- `src/gilbert/interfaces/ai.py`, `src/gilbert/interfaces/tools.py`, `src/gilbert/interfaces/attachments.py`
- `src/gilbert/core/services/ai.py`
- `std-plugins/anthropic/anthropic_ai.py`
- `frontend/src/components/chat/TurnBubble.tsx`
- `frontend/src/components/chat/ChatPage.tsx`
