# AI Prompts Are Always Configurable

## Summary
Every AI prompt in Gilbert MUST be exposed as a `ConfigParam(multiline=True, ai_prompt=True)` on the owning service. Hardcoded prompts in source are a regression — even short ones get a config knob with the bundled string as `default`.

## Details

### The rule
If you write Python that passes a non-trivial string to an AI call (`AISamplingProvider.complete_one_shot(system_prompt=...)`, `AIService.chat(system_prompt=...)`, `Message(role=SYSTEM, content=...)`, etc.), that string lives behind a `ConfigParam`. No exceptions for "this one is short" or "users won't want to edit this." The rule exists so:

- Operators can tune behavior without redeploying.
- The Settings UI's "Author with AI" button can rewrite prompts live (only fires when `ai_prompt=True`).
- Prompt drift between code and runtime is impossible — the running value is always the configured value.

### Pattern to follow

```python
# Module-level constant — the bundled default.
_DEFAULT_FOO_PROMPT = """\
You are ...
"""

class FooService(Service):
    def __init__(self) -> None:
        ...
        self._foo_prompt: str = _DEFAULT_FOO_PROMPT  # cache for hot path

    def config_params(self) -> list[ConfigParam]:
        return [
            ...,
            ConfigParam(
                key="foo_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for the foo AI call. Drives <what it controls>. "
                    "Leave blank to use the bundled default."
                ),
                default=_DEFAULT_FOO_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        ...
        self._foo_prompt = (
            str(config.get("foo_prompt", "") or "") or _DEFAULT_FOO_PROMPT
        )

    async def _do_thing(self) -> None:
        await ai.complete_one_shot(
            messages=[Message(role=MessageRole.USER, content=user_msg)],
            system_prompt=self._foo_prompt,   # <-- never reference the constant directly
            profile_name=self._ai_profile,
        )
```

Key points:
- **`default` is the constant**, not an empty string. The Settings UI shows the default when the user hasn't overridden, and "Author with AI" sees the real prompt as input.
- **Falsy override falls back to the constant**: `str(config.get("foo_prompt", "") or "") or _DEFAULT_FOO_PROMPT`. Empty-string overrides shouldn't break the service — they should restore the default.
- **Cache the active prompt on `self`** in `on_config_changed`, then read `self._foo_prompt` at the call site. Don't re-read config every call, and never reference the `_DEFAULT_*` constant from the call site.
- **Description should mention what the prompt controls** — operators tuning it via the UI need that hint.

### Backend-declared prompts
If a backend (e.g. an `AIBackend`, `TTSBackend`) declares a prompt via `backend_config_params()`, the *owning service* is responsible for forwarding `ai_prompt=bp.ai_prompt` when it wraps backend params into its own `ConfigParam` list. See [Configuration Service](memory-configuration-service.md) "Backend Registry Pattern" for the affected wrapper sites — adding a new wrapper means propagating this flag too.

### Acceptable exceptions (small list)
A few prompts are intentionally NOT exposed:
- Backend connection-test probes like `"Reply with a single word."` — they exist to verify the wire works, not to drive behavior.
- Prompts whose output structure is parsed by code that would break on a different shape (e.g. JSON schema instructions tightly coupled to a parser). Even then, prefer to expose them and rely on the description to warn the user, rather than hide them.

When in doubt: expose it. Recovery from a bad edit is one Reset button click; recovery from a hidden hardcoded prompt requires a code change.

### Architecture audit hook
The "Hardcoded AI prompt" check is part of the architecture violation checklist (see [Architecture Violation Checklist](memory-architecture-checklist.md)). When the user asks to audit the code, grep for `system_prompt=` literal strings and `_DEFAULT_*PROMPT` constants and verify each is wired to a `ConfigParam(ai_prompt=True)`.

## Related
- [Configuration Service](memory-configuration-service.md) — `ConfigParam.ai_prompt` flag and `config.prompt.author` WS handler
- [AI Service](memory-ai-service.md) — `AISamplingProvider.complete_one_shot` and `chat` are the two entry points where prompts are passed
- `frontend/src/components/settings/AuthorPromptDialog.tsx` — the modal that uses `ai_prompt=True` fields
