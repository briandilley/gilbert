# Persona Service

## Summary
Manages the AI assistant's personality, tone, and behavioral instructions. Stored in the entity system, editable at runtime via AI tools. The AI service reads the active persona to build its system prompt dynamically.

## Details

### Service
- `src/gilbert/core/services/persona.py` — `PersonaService`
- Capabilities: `persona`, `ai_tools`
- Requires: `entity_storage`
- Implements `Configurable` protocol with `config_namespace = "persona"` (category: "Intelligence")
- Exposes a `default_persona` config param (multiline string) so the default persona text can be viewed/edited via the ConfigurationService web UI
- Always registered (not optional) — AI service depends on it
- Stores persona in `persona` collection, entity ID `active`
- Tracks `is_customized` flag — False until user explicitly updates

### Default Persona
- Defined as `DEFAULT_PERSONA` constant in the service module
- Casual, friendly, professional, slightly sarcastic
- Instructions for announcements (natural intros, varied each time)
- Instructions for tool use (don't leak config details)
- Only describe capabilities matching available tools — if a tool isn't available for the user's role, don't claim the capability exists; suggest asking an admin or logging in with higher privileges

### AI Integration
- AI service declares `persona` as a required capability
- `_build_system_prompt()` prepends persona text before config system_prompt
- When `is_customized` is False, appends a one-time nudge telling the user they can customize the persona
- Config `system_prompt` is now empty by default — persona carries all personality

### Tools
- `get_persona` — returns current persona text
- `update_persona` — replaces persona text, sets customized=True
- `reset_persona` — reverts to DEFAULT_PERSONA, sets customized=False

## Related
- `src/gilbert/core/services/ai.py` — consumes persona in system prompt
- `tests/unit/test_persona_service.py` — 11 unit tests
