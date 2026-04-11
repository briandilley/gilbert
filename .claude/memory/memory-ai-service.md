# AI Service

## Summary
Central AI service that orchestrates conversations with tool use. Uses the AIBackend ABC + backend registry pattern. Currently uses Anthropic Claude via direct httpx calls. System prompt comes from PersonaService, not AI config.

## Details

### Architecture Layers
- **`interfaces/tools.py`** — `ToolProvider` protocol (runtime_checkable), `ToolDefinition`, `ToolCall`, `ToolResult`, `ToolParameterType`
- **`interfaces/ai.py`** — `AIBackend` ABC (with registry pattern), `Message`, `MessageRole`, `AIRequest`, `AIResponse`, `StopReason`, `TokenUsage`
- **`core/services/ai.py`** — `AIService(Service)` — the orchestrator
- **`integrations/anthropic_ai.py`** — `AnthropicAI(AIBackend)` — Claude via httpx

### AIService
- **Capabilities:** `ai_chat`
- **Requires:** `entity_storage`, `persona`
- **Optional:** `ai_tools`, `configuration`, `access_control`
- **Main method:** `chat(user_message, conversation_id=None) -> (response_text, conversation_id)`
- **Agentic loop:** Calls backend.generate(), executes tool calls, feeds results back, repeats up to `max_tool_rounds`
- **Lazy tool discovery:** Tools discovered at each chat() call via `resolver.get_all("ai_tools")`, not during start(). Avoids startup ordering issues and picks up dynamically-loaded plugins.
- **Conversation persistence:** Stored in `ai_conversations` collection in document storage. Messages serialized/deserialized with tool calls and results.
- **History truncation:** Keeps last `max_history_messages`, never splits tool-call/result pairs.

### ToolProvider Protocol
Any service declaring `ai_tools` capability that implements `tool_provider_name`, `get_tools()`, and `execute_tool()` is auto-discovered.

### AnthropicAI Backend
- Direct HTTP via httpx.AsyncClient (no anthropic SDK dependency)
- Translates Message objects to Anthropic content block format
- System prompt passed as separate `system` parameter
- TOOL_RESULT messages become user messages with `tool_result` content blocks
- Backend-specific params: `model`, `max_tokens`, `temperature` — declared via `backend_config_params()`, stored under `settings.*` in entity storage
- API key is a backend param (`settings.api_key`, marked sensitive), not a credential reference

### Configuration
AIService implements `Configurable` with `config_category = "Intelligence"`. Service-level params:
- `enabled` — whether the AI service is active (restart required)
- `backend` — backend provider name (e.g., `"anthropic"`, restart required, choices from registry)
- `max_history_messages` — conversation history window (default 50)
- `max_tool_rounds` — max agentic loop iterations (default 10)

Backend params are merged under `settings.*` prefix with `backend_param=True`. For Anthropic: `settings.model`, `settings.max_tokens`, `settings.temperature`, `settings.api_key`.

System prompt is managed by PersonaService, not AI config.

## Related
- [Service System](memory-service-system.md)
- [Persona Service](memory-persona-service.md)
- [Storage Backend](memory-storage-backend.md)
- [Configuration Service](memory-configuration-service.md)
- `src/gilbert/interfaces/ai.py`, `src/gilbert/interfaces/tools.py`
- `src/gilbert/core/services/ai.py`
- `src/gilbert/integrations/anthropic_ai.py`
