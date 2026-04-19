# Backend Pattern

## Summary
All swappable components (AI, TTS, auth, speakers, music, doorbell, etc.) follow a universal backend pattern: an ABC with an `__init_subclass__` registry, `backend_name` identifier, `backend_config_params()` for UI, and `initialize()`/`close()` lifecycle. Services discover backends through the registry after a side-effect import — never by direct concrete import.

## Details

### The Pattern

Backend ABCs declare:

- **`backend_name: str = ""`** — class attribute used as registry key
- **`_registry: dict[str, type]`** — populated by `__init_subclass__`
- **`backend_config_params() -> list[ConfigParam]`** — classmethod declaring backend-specific settings (rendered in the Settings UI under "Backend Settings" with `backend_param=True`)
- **`initialize(config) / close()`** — lifecycle hooks

Concrete subclasses set `backend_name = "elevenlabs"` (etc.) and get auto-registered on class definition.

### Discovery by services

Services must never directly import concrete backend classes. The correct recipe:

1. Side-effect import to trigger `__init_subclass__`:
   ```python
   try:
       import gilbert.integrations.elevenlabs_tts  # noqa: F401
   except ImportError:
       pass
   ```
   For plugin-hosted backends the `plugin.py`'s `setup()` does the side-effect import.

2. Look up by name from the registry:
   ```python
   backends = TTSBackend.registered_backends()
   cls = backends.get("elevenlabs")
   if cls:
       backend = cls()
       await backend.initialize(config)
   ```

### Anti-pattern

```python
# WRONG — bypasses the registry and couples core to a concrete class
from gilbert.integrations.elevenlabs_tts import ElevenLabsTTS
backend = ElevenLabsTTS()
```

Only `app.py` (composition root) and tests are allowed to import concrete backends.

### Backend ABCs following this pattern

`AIBackend`, `TTSBackend`, `AuthBackend`, `UserProviderBackend`, `TunnelBackend`, `VisionBackend`, `DocumentBackend`, `EmailBackend`, `MusicBackend`, `SpeakerBackend`, `DoorbellBackend`, `WebSearchBackend`.

Only vendor-free backends (`LocalAuth`, `LocalDocuments`) live in `src/gilbert/integrations/`; every third-party integration is a std-plugin under `std-plugins/`.

### AI Backend streaming surface

`AIBackend` has two optional surfaces on top of the base `generate()` contract so core code can branch on backend support without provider-specific `isinstance` checks:

- **`capabilities() -> AIBackendCapabilities`** — advertises `streaming` and `attachments_user` flags. Default returns both `False`; backends override to opt in.
- **`generate_stream(request) -> AsyncIterator[StreamEvent]`** — yields provider-neutral `StreamEvent`s (`TEXT_DELTA`, `TOOL_CALL_START`, `TOOL_CALL_DELTA`, `TOOL_CALL_END`, `MESSAGE_COMPLETE`). The default implementation calls `generate()` and yields a single `MESSAGE_COMPLETE`, so non-streaming backends compose with the core loop for free.

`AIService.chat()` drives the backend via `generate_stream()` unconditionally and forwards `TEXT_DELTA`s onto the event bus as `chat.stream.text_delta`. All Anthropic-specific SSE parsing and event names live in `std-plugins/anthropic/anthropic_ai.py` — core never imports from the plugin. Adding a new streaming backend (OpenAI, Gemini, local llama.cpp) means implementing `generate_stream()` on its own `AIBackend` subclass — nothing outside that file changes.

### Why the side-effect import

The registry only knows about backends whose class body has executed. Having the service `import backend_module` (even via `# noqa: F401`) is how we tell Python to run the class definitions so `__init_subclass__` fires. A missing side-effect import silently hides a backend from the registry.

## Related
- `src/gilbert/interfaces/ai.py` — `AIBackend`, `AIBackendCapabilities`, `StreamEvent`
- `src/gilbert/interfaces/tts.py` — `TTSBackend`
- `src/gilbert/interfaces/auth.py` — `AuthBackend`, `UserProviderBackend`
- [Configuration Service](memory-configuration-service.md) — how `backend_config_params()` feeds the Settings UI
- [Multi-backend Aggregator Pattern](memory-multi-backend-pattern.md) — one service + N backends, not N services
- [AI Service](memory-ai-service.md) — how `generate_stream` is consumed by the agentic loop
