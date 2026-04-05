# AI Service

## Summary
Central AI service that orchestrates conversations with tool use. Follows the same AIBackend ABC + AIService wrapper pattern as TTS. Currently uses Anthropic Claude via direct httpx calls.

## Details

### Architecture Layers
- **`interfaces/tools.py`** — `ToolProvider` protocol (runtime_checkable), `ToolDefinition`, `ToolCall`, `ToolResult`, `ToolParameterType`
- **`interfaces/ai.py`** — `AIBackend` ABC, `Message`, `MessageRole`, `AIRequest`, `AIResponse`, `StopReason`, `TokenUsage`
- **`core/services/ai.py`** — `AIService(Service)` — the orchestrator
- **`integrations/anthropic_ai.py`** — `AnthropicAI(AIBackend)` — Claude via httpx

### AIService
- **Capabilities:** `ai_chat`
- **Requires:** `credentials`, `entity_storage`
- **Optional:** `ai_tools`
- **Main method:** `chat(user_message, conversation_id=None) -> (response_text, conversation_id)`
- **Agentic loop:** Calls backend.generate(), executes tool calls, feeds results back, repeats up to `max_tool_rounds`
- **Lazy tool discovery:** Tools discovered at each chat() call via `resolver.get_all("ai_tools")`, not during start(). Avoids startup ordering issues and picks up dynamically-loaded plugins.
- **Conversation persistence:** Stored in `ai_conversations` collection in document storage. Messages serialized/deserialized with tool calls and results.
- **History truncation:** Keeps last `max_history_messages`, never splits tool-call/result pairs.

### ToolProvider Protocol
Any service declaring `ai_tools` capability that implements `tool_provider_name`, `get_tools()`, and `execute_tool()` is auto-discovered. Built-in tool providers: StorageService, EventBusService, TTSService.

### AnthropicAI Backend
- Direct HTTP via httpx.AsyncClient (no anthropic SDK dependency)
- Translates Message objects to Anthropic content block format
- System prompt → separate `system` parameter
- TOOL_RESULT messages → user messages with `tool_result` content blocks
- Logs to `gilbert.ai` logger

### Configuration
```yaml
ai:
  enabled: false
  backend: anthropic
  credential: ""  # name of an api_key credential
  system_prompt: "You are Gilbert..."
  max_history_messages: 50
  max_tool_rounds: 10
  settings:
    model: claude-sonnet-4-20250514
    max_tokens: 4096
    temperature: 0.7
```

## Related
- [Service System](memory-service-system.md)
- [Credential Service](memory-credential-service.md)
- [Storage Backend](memory-storage-backend.md)
- `src/gilbert/interfaces/ai.py`, `src/gilbert/interfaces/tools.py`
- `src/gilbert/core/services/ai.py`
- `src/gilbert/integrations/anthropic_ai.py`
