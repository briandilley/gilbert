# Context Map

Gilbert is documented as **two bounded contexts**, split along the real boundary that already
exists in the codebase: the core platform versus the first-party plugins (a separate repo mounted
as a submodule). Each context owns its glossary (`CONTEXT.md`) and its decision records (`docs/adr/`).

Start here, then read the relevant context's `CONTEXT.md` before exploring or proposing work. How
the engineering skills should consume these docs lives in [`docs/agents/domain.md`](./docs/agents/domain.md).

## Contexts

- **[Core](./src/gilbert/CONTEXT.md)** (`src/gilbert/`, repo `briandilley/gilbert`) — the assistant
  platform. The backend/registry pattern, capability protocols, the discoverable-service system,
  the AI/agent loop and AI context profiles, storage, configuration, and the thin web/WS layer.
  Decisions: [`docs/adr/`](./docs/adr/) (system-wide / core).
- **[Plugins](./std-plugins/CONTEXT.md)** (`std-plugins/`, repo `briandilley/gilbert-plugins`,
  mounted as a git submodule) — first-party integrations. One directory per integration, each a
  virtual uv workspace member that registers backends/services against the Core interfaces.
  Decisions: [`std-plugins/docs/adr/`](./std-plugins/docs/adr/) (plugin-scoped).

## Relationships

- **Core → Plugins (one-way dependency).** Plugins import only from `gilbert.interfaces.*`; Core
  never imports a plugin. The contract between them is the set of ABCs and `@runtime_checkable`
  capability protocols in `src/gilbert/interfaces/`.
- **Integration via the backend registry + service manager.** A plugin registers a backend by
  subclassing the relevant ABC (auto-registered via `__init_subclass__`) or registers a service
  through its `PluginContext`. Core discovers both by name/capability at runtime — not by import.
- **Shared vocabulary.** Both contexts speak **backend**, **service**, **capability**,
  **ConfigParam**, and **tool**. Their definitions live in the Core glossary; the Plugins glossary
  covers the terms unique to authoring and packaging a plugin.

## Decision records

- `docs/adr/` — Core / system-wide decisions (`0001-…`).
- `std-plugins/docs/adr/` — Plugin-system decisions (`0001-…`), versioned with the submodule.

> Other documentation genres (subsystem walkthroughs in `docs/architecture/`, design specs,
> how-to runbooks, historical plans) are **reference, not glossary/ADR** — see
> [`docs/agents/domain.md`](./docs/agents/domain.md) for how to treat them.
