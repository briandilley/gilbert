# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the
codebase. **This is a multi-context repo** — see `CONTEXT-MAP.md` at the root.

## Before exploring, read these

1. **`CONTEXT-MAP.md`** (repo root) — lists the contexts and how they relate. Read it first to
   decide which context your topic belongs to.
2. The **`CONTEXT.md`** for the relevant context (the map links each one):
   - **Core** → [`src/gilbert/CONTEXT.md`](../../src/gilbert/CONTEXT.md) — the assistant platform:
     interfaces/ABCs, services, the backend registry, the AI/agent loop, storage, config, web.
   - **Plugins** → [`std-plugins/CONTEXT.md`](../../std-plugins/CONTEXT.md) — first-party
     integrations (a separate repo, `gilbert-plugins`, mounted as the `std-plugins/` submodule).
3. **ADRs** — read the ones that touch the area you're about to work in:
   - **Core / system-wide** decisions → `docs/adr/`
   - **Plugin-scoped** decisions → `std-plugins/docs/adr/`

If any of these files don't exist yet, **proceed silently**. Don't flag their absence or suggest
creating them upfront — `/grill-with-docs` creates them lazily as terms and decisions get resolved.

## Other doc genres (reference only — NOT glossary or ADR)

This repo predates the glossary/ADR system and keeps several other doc kinds. They are valuable
**reference** but are *not* the matt-pocock genres — don't treat them as the source of canonical
vocabulary or decisions, and don't try to reshape them:

- **`docs/architecture/`** (core) and **`std-plugins/docs/architecture/`** (plugins) — subsystem
  *walkthroughs* and gotchas. Read the relevant one before working in a subsystem (e.g.
  `agent-service.md` before the agent loop, `speaker-system.md` before speaker code). When a
  decision in one of these has been distilled into an ADR, the ADR is canonical; the walkthrough
  is the long-form explanation.
- **`docs/specs/`** and **`docs/superpowers/specs/`** — feature design docs.
- **`docs/how-to/`** — operator runbooks (e.g. external-service setup).
- **`docs/plans/`** and **`docs/superpowers/plans/`** — historical implementation plans.

## Use the glossary's vocabulary

When your output names a domain concept (an issue title, a refactor proposal, a hypothesis, a test
name), use the term as defined in the relevant context's `CONTEXT.md`. Respect the `_Avoid_` lists —
don't drift to synonyms the glossary explicitly rejects (e.g. say **backend**, not "driver" or
"provider class"; **profile** when you mean an `AIContextProfile`, disambiguated from a user profile).

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing
language the project doesn't use (reconsider) or there's a real gap (note it for `/grill-with-docs`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0002 (capability protocols over concrete service imports) — but worth reopening
> because…_
