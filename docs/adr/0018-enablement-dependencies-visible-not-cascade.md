# Enablement dependencies surface as a visible disabled state, never auto-enable

A service/plugin may declare an *enablement dependency* — a named backend or service that must be
**enabled** before it can run (finer-grained than a capability `requires`, which only orders startup
waves). When the prerequisite is off, the dependent **does not start** and is surfaced as
*disabled, with the reason* (a Settings badge plus a toast on the toggle attempt) rather than being
silently skipped. Gilbert never reaches over and enables the prerequisite itself.

## Considered options

- **Auto-enable cascade** (enabling X enables its prerequisites) — rejected: enabling a
  backend/service has side effects the admin didn't choose (starting a daemon, requiring
  credentials, beginning to poll), and a cascade makes "what is actually on, and why" hard to reason
  about. A visible, manual prompt is safer and clearer.
- **Today's silent skip** (unmet `requires` → service quietly never starts) — kept as the runtime
  behavior, but made *visible*: the unmet state is now reported to the operator instead of looking
  like a bug.

## Consequences

The dependency target is a new kind — "named backend/service enabled" — distinct from the existing
capability-`requires` system, because a backend being enabled (e.g. `ollama`) is not a published
service capability. The first consumer is the local-model manager plugin, which depends on the
`ollama` backend being enabled (see std-plugins ADR-0007).
