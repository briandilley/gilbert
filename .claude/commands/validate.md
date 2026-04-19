---
name: validate
description: Run the full architecture validation checklist against the Gilbert codebase
---

Run the full architecture validation checklist against the Gilbert codebase. This is a comprehensive audit — check every rule, report all violations, and fix them.

## Instructions

1. **Load context**: Read `CLAUDE.md` (the architecture reference) and all memory files from `.claude/memory/` (check `MEMORY.md` for the index, then read each referenced file).

2. **Run the Architecture Violation Checklist** from CLAUDE.md. This includes:

   - **Layer Import Violations**: Scan imports in `interfaces/`, `core/services/`, `integrations/`, `storage/`, `web/`, and all plugins. Flag any cross-layer import that violates the dependency rules.
   
   - **Concrete Class Violations**: Find `isinstance` checks against concrete service classes, direct backend instantiation, direct import of concrete backends outside `app.py`.
   
   - **Duck-Typing and Private Access Violations**: Find `getattr` used for capability access, private `._field` access across modules, unnecessary `# type: ignore` comments.
   
   - **Business Logic in Wrong Layer**: Check web routes for authorization logic, AI prompt construction, backend resolution. Check for shared constants defined in wrong layers.
   
   - **Plugin-Specific Checks**: Verify plugins use `resolver.get_capability()` not concrete imports, implement `Configurable` for runtime config, don't access private attributes, and declare `slash_namespace`.
   
   - **Slash Command Violations**: Audit every `ToolDefinition` for missing `slash_command`, missing `slash_help`, services with 3+ tools not using `slash_group`, hostile parameter ordering, invalid identifiers, duplicates.
   
   - **Documentation Freshness**: Check `README.md`, `std-plugins/README.md`, `std-plugins/CLAUDE.md`, and `CLAUDE.md` for drift from current code.

3. **Check memory-derived rules**: Apply any feedback or project memories that describe architectural constraints or conventions. These are rules the team has established that may not be in CLAUDE.md.

4. **Check AI backend visibility rule**: Verify that only AIService, AI profiles, and the chat UI know about AI backends. No service caller should reference backend names, model IDs, or backend classes directly.

5. **Run tests**: Execute `uv run pytest tests/ -x -q` to verify nothing is broken.

6. **Report**: List all violations found, grouped by category. For each violation, state the file, line, what's wrong, and fix it immediately.
