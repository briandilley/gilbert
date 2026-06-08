# Adopt a multi-context CONTEXT.md + ADR documentation system

Gilbert's domain language and design rationale lived only in ~30 subsystem walkthroughs under
`docs/architecture/`, with no canonical glossary and decisions buried in prose. We adopted the
matt-pocock skills' documentation model — a per-context `CONTEXT.md` glossary plus numbered ADRs —
split into **two bounded contexts** (Core in this repo, Plugins in the `std-plugins` submodule) via
a root `CONTEXT-MAP.md`. The glossaries are now the canonical vocabulary and the ADRs the canonical
decision log; the `docs/architecture/` walkthroughs remain as long-form reference.

## Considered options

- **Single root `CONTEXT.md`** — rejected: the plugins submodule is a separate repo
  (`gilbert-plugins`) with its own vocabulary and decision lifecycle, so its glossary/ADRs version
  with it.
- **Leave everything in `docs/architecture/`** — rejected: no canonical term list, and decisions
  stay tangled with walkthrough prose where the next reader can't find or trust them.
