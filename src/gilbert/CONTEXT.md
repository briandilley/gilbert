# Core

The Gilbert platform: the assistant itself, the backend/service framework that makes it
extensible, and the AI/agent machinery on top. This glossary is the canonical vocabulary for
core (`src/gilbert/`). Plugin-authoring terms live in the [Plugins glossary](../../std-plugins/CONTEXT.md);
see [`CONTEXT-MAP.md`](../../CONTEXT-MAP.md).

## Platform & architecture

**Backend**:
A swappable concrete implementation of an abstract capability (storage, AI, TTS, speaker, weather…),
auto-registered by name. Overloaded — disambiguate: the *registry class* (a kind of backend) vs. a
*named, configured instance* keyed by its `backend_name` (e.g. `sonos`, `open-meteo`), several of
which one service may run at once.
_Avoid_: driver, provider class, adapter.

**Service**:
A discoverable, long-lived component that wires backends together and exposes capabilities, AI
tools, and WebSocket RPCs. Distinct from a Backend (the vendor implementation a service drives).
_Avoid_: manager, controller, module.

**Capability** (a.k.a. **Provider**):
A named abstract behavior one service consumes from another by resolving it by string name and
checking it against a `@runtime_checkable` Protocol — never by importing the concrete service
class. Protocol names end in `Provider` (`PresenceProvider`, `FeedsProvider`).
_Avoid_: interface (ambiguous), dependency.

**Integration**:
A *vendor-free* backend that ships in core (`integrations/`) because it has no third-party
dependency (e.g. the local speaker, browser speaker, local Whisper). Not to be confused with the
general English sense — here it names the layer and its inhabitants.
_Avoid_: connector.

**Plugin** / **std-plugin**:
A self-contained extension in its own directory that registers backends and/or services against
the core interfaces. A *std-plugin* is a first-party one bundled in the `std-plugins/` submodule.
The dividing line from an Integration is purely "has a third-party vendor dependency." See the
[Plugins glossary](../../std-plugins/CONTEXT.md).

**Backend registry**:
The runtime catalog of backend implementations, populated automatically when a backend subclass is
imported (a load-bearing side-effect import). Consumers discover backends by name, never by
importing the concrete class.

**Entity store**:
Gilbert's generic, non-SQL-shaped persistence: typed collections of entities queried through a
query interface. A new entity type needs no schema migration.
_Avoid_: database (reserve for the underlying SQLite backend), table, model.

**Two-tier configuration**:
`gilbert.yaml` holds the handful of settings that must exist before storage is up (storage,
logging, web); everything else is runtime-editable config living in the `gilbert.config` entity
collection and managed at `/settings`.
_Avoid_: settings file (ambiguous between the two tiers).

**ConfigParam** / **Configurable**:
A single typed, operator-editable setting (`ConfigParam`) declared by a service or backend that
implements `Configurable`. Every non-trivial AI prompt is exposed this way.

**Enablement dependency**:
A declared prerequisite that one service/plugin needs *enabled* before it can run — a named backend
or service being on. Finer-grained than a capability `requires` (which only orders startup waves):
when the prerequisite is off, the dependent **does not start** and is shown *disabled, with the
reason* (a Settings badge + a toast on the toggle attempt). Gilbert never auto-enables the
prerequisite — enabling a backend/service has side effects (daemons, credentials, polling) the
admin must choose deliberately.
_Avoid_: dependency (ambiguous), requires (reserve for the capability-wave sense).

**Event bus**:
The internal pub/sub channel carrying typed events (`presence.arrived`, `camera.event.detected`,
`notification.received`…) whose visibility is governed by a prefix-based ACL.
_Avoid_: message queue, signals (reserve "signal" for an agent InboxSignal).

**Role**:
A caller's RBAC tier — `admin` / `user` / `guest` / `everyone`, plus the synthetic `system` — that
gates tools, events, and account access.

**UserContext**:
The explicit identity object (roles, timezone, the `SYSTEM` sentinel) threaded through service
calls. Admin-ness is *derived* from it, never passed as an ad-hoc boolean.

**Multi-user isolation**:
The discipline that singleton services keep no per-user state on `self` (every call re-reads
storage), pass identity explicitly rather than via an ambient context variable, and hand spawned
tasks a copied context so one user's request can't mutate another's.

**Host resources**:
Total/available RAM, GPU presence, and per-GPU VRAM for the machine Gilbert runs on, exposed via the
`HostResourcesProvider` capability from a vendor-free probe. Best-effort and **localhost-only**: it
describes the Gilbert host, which equals the model-serving host only when the runtime (e.g. Ollama)
is local. Raw data — interpreting it into a runnability verdict is the consumer's policy.
_Avoid_: hardware (vague), system info.

## The assistant

**Gilbert**:
The primary, global assistant persona — the default actor on every screen, addressed by the
mention pseudo-id `gilbert`. Has no row in the user table. Contrast with an Agent (a user-created
sub-persona).

**Soul**:
The *values and principles* layer of a persona — "who Gilbert is." Admin-owned, with an optional
per-user override. Paired with, but distinct from, Identity.

**Identity**:
The *persona / voice / style* layer — "how Gilbert behaves" — in three sub-layers: immutable
(admin safety rules, never overridable), default (admin persona), and per-user override. "Identity"
also loosely names the whole Soul+Identity subsystem.

**Memory** (a.k.a. **memory scope**):
A deliberately-stored fact the assistant recalls into its system prompt, in one of two scopes:
*user* (private) or *global* (everyone-visible, admin-write-only). Distinct from conversation
history and from the Soul/Identity persona (facts, not personality).
_Avoid_: knowledge (reserve for the document store), context.

## AI calls, tools & skills

**AIContextProfile** (a.k.a. **profile**):
A named bundle of (tool allowlist mode + tools + per-tool roles + pinned backend + pinned model)
that every AI call resolves through. Overloaded — disambiguate from a *user profile* (a person's
account). When unqualified, "profile" means the AIContextProfile.
_Avoid_: preset, config (for the AI-routing sense).

**Tier** (`light` / `standard` / `advanced`):
The three built-in profile names, mapping to fast / balanced / most-capable models. Tiers are just
profiles admins bind to real backends and models.

**Per-model config**:
Per-`(backend, model)` settings owned by the AI service — an `enabled` flag plus generation defaults
(`max_tokens`, `temperature`, context window), seeded from Hugging Face / GGUF metadata when a model
is pulled. Generation params resolve in layers: *backend default ← per-model ← profile ← call*.
Distinct from a backend's global config (now just the default layer) and from the AIContextProfile
(the use-case layer).
_Avoid_: model settings (vague), model overrides.

**ai_call** vs **ai_profile**:
Two ways a call selects a profile. `ai_call` is the legacy named-use-case routed through an
assignment table; `ai_profile` is the newer direct profile-name selection a service declares as
config. New code uses `ai_profile`.

**tools_override**:
A call-site parameter that forces a specific (often empty) toolset regardless of the resolved
profile. Pure-text calls pass `[]` so the model writes text instead of invoking side-effecting
tools. See [ADR-0010](../../docs/adr/0010-pure-text-ai-calls-force-zero-tools.md).

**AI tool** (a.k.a. **ToolDefinition**):
A capability exposed to the model (and often as a slash command) with flags like `parallel_safe`,
`ai_visible`, and `required_role`.

**UIBlock** (preview-confirm flow):
A structured interactive payload a tool returns *instead of acting* — Confirm/Cancel, select-from-
list, two-step literal-confirm. Mutating tools preview before they execute.
_Avoid_: form, widget.

**Skill**:
A user-enabled, per-conversation capability bundle (a `SKILL.md` plus bundled files/scripts) whose
instructions are injected into the system prompt and whose tools are gated behind explicit
activation. Overloaded — distinct from a Claude/agent skill in tooling outside Gilbert.

**Skill workspace**:
A per-`(user, skill, conversation)` directory holding files generated or uploaded during a chat,
isolated so parallel conversations don't collide.

**Activation gate**:
The hard rule that an AI-initiated skill-tool call is refused unless the skill is on the
conversation's active list. Slash invocations and system callers bypass it.
See [ADR-0012](../../docs/adr/0012-skill-activation-gate.md).

**Slash command**:
A user-typed `/tool …` invocation treated as explicit user intent — it bypasses the activation gate
and can reach slash-only, non-AI-visible tools.

**MCP** (client side vs server side):
The Model Context Protocol, used both ways. Overloaded — disambiguate: Gilbert as *client*
consuming external MCP servers (their tools namespaced into its pipeline), vs Gilbert as *server*
exposing its own tools to external agents. "MCP server" can mean either an upstream server or
Gilbert-as-server.

**Sampling** (MCP):
An external MCP server asking Gilbert for an AI completion, gated by an allow flag, a sampling
profile, and a token budget.

## Subagents

**Subagent** — an *ephemeral, headless* agent run spawned within a chat turn
  (the `SubagentService` engine): a fresh context (shared preamble + a
  *SubagentType* system prompt + the task), a scoped toolset + model (from the
  type's profile), and a bounded budget. It runs autonomously and **cannot ask
  the user** — its final message is returned as the spawning tool's result
  (inline) or delivered as a report file attachment (background). _Avoid_
  calling a subagent an "agent" unqualified.

**SubagentType** — an entity-backed, admin-managed agent definition stored in
  the `subagent_types` collection. A type carries a system prompt, a referenced
  AI profile (which owns model selection *and* tool gating), round/time budget,
  execution mode (`sync` | `background`), and delivery mode (`inline` |
  `report_file`). Built-in types (e.g. `deep-research`, `software-engineer`)
  are seeded on first run and can be edited or reset; custom types can be
  deleted. Each built-in type references a same-named seeded AI profile that
  carries its toolset. Model/tool selection is **admin-selected data**, not a
  user-visible AI-backend detail — see ADR-0021. The dataclass lives in
  `interfaces/subagent.py` (shared data) and is read via the `SubagentCatalog`
  capability. _Avoid_: "agent profile", "agent template". The correct term is
  "subagent type".

**RunSpec / AgentRunEngine** — the shared single-agent run primitive in
  `core/agent_run/`. A `RunSpec` is the fully-resolved description of one run
  (system prompt, profile/model, tool filter, round + wall-clock budget,
  `headless` flag, callbacks); `AgentRunEngine.run()` drives the chat turn,
  enforces the wall-clock deadline, runs the budget-exhaustion synthesis
  fallback, and emits lifecycle events. `SubagentService.spawn()` builds a
  RunSpec and calls it.

## Conversation

**@-mention**:
A structured `@[Display Name](user_id)` tag stored inline in shared-room message content, rendered
as a chip and resolved to a notified-user list. The `gilbert` pseudo-id addresses the assistant.

**Shared room** (a.k.a. **shared conversation**):
A multi-member chat where mentions, member RBAC, and unread-mention tracking apply — as opposed to
a personal chat, where the mention picker stays inert.

**Work transcript**:
The design framing of the chat UI as a scannable work *log* (rail rows, mono-rail tool calls)
rather than a peer-conversation bubble product.
_Avoid_: chat log, thread.

## Self-improvement

**Observation**:
A persisted signal of a capability gap, frustration, or pattern, gathered from several sources —
the raw material for reflection.

**Reflection**:
The periodic cycle that feeds accumulated Observations to the AI to generate Proposals, grounded in
the real code.

**Proposal**:
A structured self-improvement record the AI emits during reflection (new plugin / modify plugin /
config change / modify core), persisted for admin triage.

## Voice & audio

**Speaker**:
Overloaded — disambiguate: (a) an *audio-output target* a SpeakerBackend controls (a Sonos unit,
the host's `local` virtual speaker, a user's browser tab); (b) in presence/wake-word context, the
*person talking*. As a device it's namespaced `<backend>:<native_id>`.

**Browser speaker**:
A user's connected SPA tab acting as a private, per-user audio output (`browser:<user-id>`), live
only while the user's toggle is on and a WebSocket is open.

**Announce** (a.k.a. **announcement**):
A short spoken interjection played over speakers with duck-play-restore semantics — distinct from
normal media playback.

**Primary backend**:
The configured default speaker backend used for discovery and grouping when an AI tool call names no
explicit backend. Distinct from the set of all loaded backends.

**Wake word**:
A spoken trigger phrase detected by a WakeWordBackend — a third transcription role alongside batch
and streaming.

**Transcription role** (`batch` / `streaming` / `wake-word`):
The three sibling speech-to-text backend shapes the transcription service routes among: one-shot
bytes→text, session streaming, and wake-word detection.

## Presence & arrival

**Presence**:
A user's composite location status (`present` / `nearby` / `away` / `unknown`) derived by
aggregating signals (badge, face, WiFi) into one per-user state.

**GreetingContextProvider**:
A capability by which any service contributes a labeled prose fact (weather, briefing, health) that
gets folded into the arrival-greeting prompt.

**Greeting** / **briefing** / **digest**:
Presence-triggered or scheduled summaries — a *greeting* on arrival, a feeds *briefing*, a daily
weather *digest*.

## Knowledge & feeds

**Knowledge store**:
Gilbert's document corpus — a multi-backend store that indexes documents into a vector index for
semantic search. Distinct from Memory (deliberate facts) and from feed storage.
_Avoid_: memory, corpus (informal).

**DocumentBackend** (a.k.a. **source**):
A pluggable origin of documents (a local directory, a Drive folder, the synthetic feed-articles
source), each identified by a `source_id`. Overloaded — a "feed source" (RSS/Atom) is a different
thing.

**Knowledge ingestion**:
Feeding an external item's full body into the knowledge store, after which the local bytes are
discarded. Distinct from feed *polling*, which stores only title/link/summary.

**Synthetic `feed_articles` backend**:
A DocumentBackend owned *privately* by the feeds service (never registered with the knowledge
service) so ingested articles get a `source_id` without being re-walked on every sync.

**Feed subscription**:
A user-owned, shareable RSS/Atom subscription polled by a FeedBackend. Stores only metadata + AI
summary + score, never the full article body.

**Feed scoring**:
Async AI scoring of each feed item (0–1, scaled by a per-feed importance weight); deferred items are
drained later by a lazy-score tick.

**Briefing** (a.k.a. **news briefing**):
An AI-generated daily digest of top-scored unbriefed feed items, producing both a spoken paragraph
and clickable headlines. Built by the feeds service; *who/when* is decided by the briefing
scheduler.

## Connected accounts

**Account**:
A user-owned, shareable connection to an external provider (a calendar, mailbox, task list). One
backend instance and one poll job per account; access tiers are owner / admin / shared-user /
shared-role. Overloaded — not an OS/system account.

**Mailbox** / **Inbox-AI** / **Outbox**:
A *mailbox* is a shareable email account (one polling EmailBackend). *Inbox-AI* is the per-message
AI reply/action-item flow. The *outbox* is a persisted queue of outbound drafts flushed by a tick,
for delayed or crash-resilient sends.

**Task list** / **local-first reconciliation**:
A *task list* is a user-owned, shareable to-do list bound to one TaskBackend. *Local-first
reconciliation* is the write model where a mutation lands in storage immediately, pushes upstream
inline, and is retried by a sync tick — so the AI never blocks on a slow backend.
See [ADR-0015](../../docs/adr/0015-tasks-local-first-reconciliation.md).

**Health metric** / **`health-admin` role**:
A *health metric* is a typed personal-health reading from a backend, with owner-only reads. The
`health-admin` role is a separately-seeded level-0 role — never auto-granted, not even to `admin` —
required to read another user's metrics (audited and target-notified).
See [ADR-0016](../../docs/adr/0016-health-owner-only-privacy.md).

**Media library** / **continue watching** / **link-user**:
The *media library* is a multi-backend video aggregator over Plex/Jellyfin. *Continue watching* is a
user's resume queue of partially-watched items. *Link-user* is the row mapping a Gilbert user to a
backend identity; an unmapped user is skipped, never falling back to the admin's account.

**Notification** / **push route** / **quiet hours**:
A *Notification* is a persisted, user-addressed in-app message with an urgency and source tag. A
*push route* is a user-owned external destination (ntfy, Pushover, Discord, Telegram) with an
urgency floor and *quiet hours* — a per-route window during which external delivery is suppressed.

## Operations & design

**Source update**:
The admin-only mechanism that switches the running instance to a different git branch on a locally-
configured remote, via a *sentinel file* consumed by `gilbert.sh` and a supervised restart with
*LKG* (last-known-good) auto-rollback.
See [ADR-0017](../../docs/adr/0017-source-update-supervised-branch-switch.md).

**Technical Broadsheet**:
The codename for the SPA design system — a refined dark admin aesthetic with editorial typography.
Three rules: *mono carries meaning* (monospace is reserved for technical data, never decorative
prose), *hairlines over fills*, and *one accent doing real work* (the warm-amber `--signal` color).
