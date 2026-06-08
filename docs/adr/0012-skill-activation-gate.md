# The AI may not use a skill unless the user activated it for the conversation

An AI-initiated skill-tool call is refused unless the skill is on the conversation's active list.
Only user-typed slash invocations and system callers bypass the gate. `SKILL.md` injection into the
system prompt is a *soft* signal that a skill exists; the activation gate is the *hard* rule that
keeps the AI from using it unprompted.

The assistant was silently invoking skills the user had never enabled. The gate trades some AI
self-service (it can't pull in a skill it decides it needs) for explicit, auditable user opt-in.
