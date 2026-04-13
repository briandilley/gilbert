# AI Service

## Summary
Central AI service that orchestrates conversations with tool use. Uses the AIBackend ABC + backend registry pattern. Currently uses Anthropic Claude via direct httpx calls. Includes internal helpers for persona and user memory (previously separate services).

## Details

### Architecture Layers
- **`interfaces/tools.py`** — `ToolProvider` protocol (runtime_checkable), `ToolDefinition`, `ToolCall`, `ToolResult`, `ToolParameterType`
- **`interfaces/ai.py`** — `AIBackend` ABC (with registry pattern), `Message`, `MessageRole`, `AIRequest`, `AIResponse`, `StopReason`, `TokenUsage`
- **`core/services/ai.py`** — `AIService(Service)` — the orchestrator, plus `_PersonaHelper` and `_MemoryHelper`
- **`integrations/anthropic_ai.py`** — `AnthropicAI(AIBackend)` — Claude via httpx

### AIService
- **Capabilities:** `ai_chat`, `ai_tools`, `ws_handlers`, `persona`, `user_memory`
- **Requires:** `entity_storage`
- **Optional:** `ai_tools`, `configuration`, `access_control`
- **Main method:** `chat(user_message, conversation_id=None) -> (response_text, conversation_id, ui_blocks, tool_usage)`
- **Agentic loop:** Calls backend.generate(), executes tool calls, feeds results back, repeats up to `max_tool_rounds`
- **Lazy tool discovery:** Tools discovered at each chat() call via `resolver.get_all("ai_tools")`, not during start()
- **Conversation persistence:** Stored in `ai_conversations` collection
- **History truncation:** Keeps last `max_history_messages`, never splits tool-call/result pairs

### Internal Helpers (merged services)
- **`_PersonaHelper`** — manages AI persona text in `persona` collection. Exposes tools: `get_persona`, `update_persona`, `reset_persona`
- **`_MemoryHelper`** — per-user persistent memories in `user_memories` collection. Exposes tool: `memory` (actions: remember, recall, update, forget, list). The tool handler (`_tool_memory_action`) reads caller identity from the injected `_user_id` argument — the WS chat path does not set the `get_current_user` contextvar before dispatching tool calls.

### ToolProvider Protocol
Any service declaring `ai_tools` capability that implements `tool_provider_name`, `get_tools()`, and `execute_tool()` is auto-discovered.

### AnthropicAI Backend
- Direct HTTP via httpx.AsyncClient (no anthropic SDK dependency)
- Backend-specific params: `model`, `max_tokens`, `temperature` — declared via `backend_config_params()`
- API key is a backend param (`settings.api_key`, marked sensitive)

### Configuration
AIService implements `Configurable` with `config_category = "Intelligence"`. Params:
- `max_history_messages` — conversation history window (default 50)
- `max_tool_rounds` — max agentic loop iterations (default 10)
- `backend` — backend provider name (restart required)
- `default_persona` — default persona text (multiline)
- `memory_enabled` — whether AI memory system is enabled (restart required)

Backend params are merged under `settings.*` prefix with `backend_param=True`.

## Related
- [Service System](memory-service-system.md)
- [Storage Backend](memory-storage-backend.md)
- [Configuration Service](memory-configuration-service.md)
- `src/gilbert/interfaces/ai.py`, `src/gilbert/interfaces/tools.py`
- `src/gilbert/core/services/ai.py`
- `src/gilbert/integrations/anthropic_ai.py`
