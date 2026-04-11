# Persona (AIService Internal)

## Summary
Manages the AI assistant's personality, tone, and behavioral instructions. Stored in the entity system, editable at runtime via AI tools. Implemented as `_PersonaHelper` inside `src/gilbert/core/services/ai.py` (merged into AIService, no longer a separate service).

## Details

### Implementation
- `_PersonaHelper` class in `src/gilbert/core/services/ai.py`
- AIService initializes `self._persona = _PersonaHelper(storage)` in `start()`
- AIService capabilities include `persona`
- Stores persona in `persona` collection, entity ID `active`
- Tracks `is_customized` flag — False until user explicitly updates

### Default Persona
- Defined as `DEFAULT_PERSONA` constant in `src/gilbert/core/services/ai.py`
- Casual, friendly, professional, slightly sarcastic
- Instructions for announcements (natural intros, varied each time)
- Instructions for tool use (don't leak config details)

### AI Integration
- `_build_system_prompt()` prepends persona text
- When `is_customized` is False, appends a one-time nudge telling the user they can customize the persona
- Config `default_persona` param is part of AIService's config_params (multiline string, category "Intelligence")

### Tools (exposed by AIService)
- `get_persona` — returns current persona text
- `update_persona` — replaces persona text, sets customized=True
- `reset_persona` — reverts to DEFAULT_PERSONA, sets customized=False

## Related
- `src/gilbert/core/services/ai.py` — contains _PersonaHelper and consumes persona in system prompt
- `tests/unit/test_persona_service.py` — unit tests for helper and AIService persona tools
