# Subagent Engine — Design Spec

- **Date:** 2026-06-08
- **Status:** Draft (for review)
- **Scope:** Phase 1 of a multi-phase program. This spec covers the **subagent engine** (core, default-on) and its **first agent type, `deep-research`**. Repositioning the existing durable agent system as opt-in "autonomous agents" on top of this engine is **Phase 2** and out of scope here.

## 1. Summary

Add a Claude-Code-style **subagent** capability to Gilbert: the chat AI can spawn ephemeral, headless agents — each a *fresh context* (shared preamble + an agent-type prompt + the task), a *scoped toolset and model*, and a *bounded budget* — that run autonomously and return their final message as the tool result. The chat UI shows a live card for each active subagent. The first agent type, `deep-research`, performs long-horizon web research and is the concrete answer to "add support for [Tongyi] DeepResearch": the methodology runs on Gilbert's existing web tools, with Tongyi-DeepResearch-30B as the recommended (not required) model.

This generalizes — and subsumes — the earlier bespoke "deep research service" idea: deep research becomes one agent type on a reusable engine.

## 2. Background & motivation

The request began as "add support for Alibaba-NLP/DeepResearch (Tongyi DeepResearch)." DeepResearch is three things: a model (`Tongyi-DeepResearch-30B-A3B`, MoE, Apache-2.0, hosted on OpenRouter/DashScope), an agent harness (a ReAct/IterResearch loop), and a tool set (web search, page reading, etc.). Gilbert already has the harness primitives (`agent_loop.run_loop`, `AIService.chat`) and the tools (`web_search`/`fetch_url` via the Tavily backend, the browser plugin), plus AI profiles and an `openrouter` backend that can serve the model. So the durable value is the **methodology as a reusable capability**, not porting Alibaba's framework.

In discussion this generalized to a Claude-Code-style **subagent / Task** primitive, with deep research as its first type, and a layered re-org of Gilbert's agent story:

- **Layer 1 — Subagent engine (this spec): core, default-on.** Ephemeral, headless, fresh-context runs.
- **Layer 2 — Autonomous agents (Phase 2): opt-in.** Today's durable, goal-based, persona/memory/heartbeat agents, refactored to run their turns through Layer 1, renamed, and made separately enableable.

## 3. Goals / non-goals

**Goals (v1):**

- A core `SubagentService` exposing a `spawn_agent(agent_type, prompt)` AI tool, default-on (operator-disable-able).
- Ephemeral runs with a fresh context, headless toolset (no user-interaction tools), per-run budget (rounds/tokens/wall-clock), returning the final message as the tool result.
- A code-registered **agent-type registry** with two built-ins: `general-purpose` and `deep-research`.
- A live **active-subagent card** in the chat UI driven by new `subagent.*` lifecycle events.
- The `deep-research` type: research preamble + web tools + a seeded **"Deep Research" AI profile** (Tongyi via OpenRouter), plus a thin `deep_research` / `/research` sugar tool. Add `tongyi-deepresearch-30b-a3b` to the openrouter catalog.

**Non-goals (v1 — explicit boundaries):**

- **No user-defined agent types** — built-ins only. User-editable types (entities/markdown) are a follow-up.
- **No nesting** — subagents cannot spawn subagents (one level deep). Parallel spawns from the parent are allowed.
- **Synchronous only** — the parent turn awaits the subagent. Background/async delivery (job id + notification) is a follow-up.
- **No new research tools** — `deep-research` uses the existing `web_search`/`fetch_url`; no scholar/sandbox/file-parse tools.
- **Model-agnostic loop** — we do **not** replicate Tongyi's exact trained ReAct wire-format; any capable model can drive a type.
- **Phase 2 untouched** — the existing durable `AgentService` is not modified, renamed, or migrated in this spec.

## 4. Vocabulary

- **Subagent** — an ephemeral, headless run spawned by a chat turn (or, in Phase 2, by an autonomous agent). No persistence, no identity, cannot ask the user. *This spec.*
- **Agent type** — a named, code-registered definition: `{id, description, system prompt, tool policy, AI-profile ref, budget}`. Built-ins: `general-purpose`, `deep-research`.
- **Autonomous agent** — the durable, goal-based agent (today's "Agent"). *Phase 2; unchanged here.*

> Vocabulary note: Gilbert's `CONTEXT.md` currently uses "Agent" for the durable concept. Introducing "subagent" and (in Phase 2) renaming the durable concept to "autonomous agent" is a glossary + ADR change. This spec adds **"subagent"** to the Plugins/Core glossary and records an ADR for the layering decision; the rename is deferred to Phase 2.

## 5. Architecture

```
chat turn (AIService.chat)
   └─ tool: spawn_agent(agent_type, prompt)         [AI-visible tool]
        └─ SubagentService.spawn(type, prompt, caller_ctx)
             ├─ resolve AgentType from registry
             ├─ build system prompt = PREAMBLE + type.prompt
             ├─ resolve headless tool policy (exclude interactive + spawn_agent;
             │   then apply type.tool_policy via the type's AI profile)
             ├─ emit subagent.started (parent conversation_id)
             ├─ drive a bounded agentic run on a FRESH ephemeral context
             │   (reuses AIService.chat → run_loop machinery; see §6.3)
             ├─ emit subagent.progress per round
             ├─ emit subagent.completed / subagent.failed
             └─ return final message text  ──► tool result back to parent turn
```

The engine is **first-party orchestration of existing capabilities** (AI + tools), so it lives in `core/services/` — like the greeting and agent services — not in a plugin. The only third-party piece (the Tongyi model) stays in the existing `openrouter` plugin as a catalog entry.

## 6. Components

### 6.1 `AgentType` and the type registry

A small frozen dataclass + a code registry (mirrors the built-in-profiles pattern in `ai.py`).

```python
@dataclass(frozen=True)
class AgentType:
    id: str                      # "general-purpose", "deep-research"
    description: str             # routing hint the parent LLM sees in the tool schema
    system_prompt: str           # the type-specific prompt (the DEFAULT; see §8 — configurable)
    profile_name: str            # AI profile providing model + tool gating
    tool_mode: str = "exclude"   # how the type narrows tools (on top of headless gating)
    tools: tuple[str, ...] = ()  # include/exclude list per tool_mode
    max_rounds: int = 12
    max_wall_clock_s: float | None = 300.0
    max_tokens: int | None = None
```

- **Built-ins registered in code.** `general-purpose` (broad toolset, default model) and `deep-research` (web tools only, "Deep Research" profile). Plugins may register additional types later via a registration hook (future).
- The **`description`** is surfaced in the `spawn_agent` tool's `agent_type` enum so the parent LLM can choose correctly (Claude-Code style).
- The type references a **profile** for model+backend (keeps backend names out of the type — satisfies the "only AIService/profiles know backends" rule). The **system prompt** lives on the type because profiles have no prompt field.

### 6.2 Shared preamble + headless contract

One shared, configurable **preamble** is prepended to every subagent's system prompt. It encodes the autonomy contract, e.g.:

> "You are a subagent launched to complete a single task autonomously. You cannot ask the user questions or wait for input — make reasonable assumptions and proceed. Your final message is returned verbatim as the result to the agent that launched you; it is not shown to the user directly. Be thorough, then stop."

Effective system prompt = `preamble + "\n\n" + type.system_prompt`. Both the preamble and each type's prompt are `ConfigParam(ai_prompt=True)` on `SubagentService` (see §8).

**Headless tool gating** is enforced in two layers:

1. **Universal:** exclude any tool flagged `interactive=True` and exclude `spawn_agent` itself (no nesting). This requires a small, principled addition: a new `ToolDefinition.interactive: bool = False` field (default False). Tools that need the user (future `request_user_input`, etc.) set it True. v1 has effectively no interactive tools, so this is forward-looking — but it's the correct seam, declarative and system-wide.
2. **Per-type:** the type's `tool_mode`/`tools` (via its AI profile's `tool_mode`/`tools`) further narrows (e.g. `deep-research` → include only `web_search`, `fetch_url`).

### 6.3 Execution: reuse `AIService.chat`

The engine drives **`AIService.chat(...)`** — the same path `AgentService.run_agent_now()` uses — rather than calling `run_loop` directly, to reuse streaming, tool execution + argument injection, RBAC, usage/cost recording, and budget handling. Concretely, per spawn:

```python
result = await self._ai.chat(
    user_message=prompt,
    conversation_id=None,           # FRESH ephemeral context (no parent history)
    user_ctx=caller_ctx,            # inherit caller identity for RBAC
    system_prompt=effective_prompt, # preamble + type prompt (overrides chat's default)
    ai_call=f"subagent.{type.id}",  # drives profile selection / usage tagging
    ai_profile=type.profile_name,   # model + tool gating
    max_tool_rounds=type.max_rounds,
    # headless tool exclusion applied via the resolved profile + interactive flag
)
final_text = result.response_text
```

`AIService.chat` accepts an explicit `system_prompt` override (confirmed) and an `ai_profile`; `AgentService` already passes both for headless agent runs. The engine wraps the call to emit `subagent.*` lifecycle events and to enforce the wall-clock/token budget around it.

**Open implementation question (resolve in planning, recommended default in bold):**

- *Ephemeral conversation persistence.* `chat(conversation_id=None)` creates+persists a conversation. For ephemeral subagents we **persist a conversation flagged `ephemeral`/`hidden`** (so it never appears in the user's chat list but remains inspectable for debugging), rather than adding a non-persisted path now. If `chat`'s chat-specific behaviors (state injection, greeting, history compression) prove to leak into subagent runs, the fallback is a leaner shared entrypoint (`AIService.run_agentic(...)`) wrapping `run_loop` with `get_backend()` + the tool discovery/gating helpers — but we start by reusing `chat`, matching the proven `AgentService` pattern.

### 6.4 `spawn_agent` tool + `deep_research` sugar

`SubagentService` implements `Service` + `ToolProvider` and exposes:

- **`spawn_agent(agent_type: enum, prompt: string)`** — `ai_visible=True`, `parallel_safe=True` (parent may fan out). `agent_type` enum + descriptions come from the registry. Returns the subagent's final message. `required_role` gates who/what can spawn. The tool is itself flagged so it's excluded from subagent toolsets (no nesting).
- **`deep_research(query: string, depth?: enum)`** + `/research <query>` — thin sugar that calls `spawn(type="deep-research", prompt=query, ...)`. `depth` maps to a rounds/budget tier. Exists for discoverability (a real slash command, an obvious tool the model reaches for); shares all machinery with `spawn_agent`.

### 6.5 Live UI: active-subagent card

New lifecycle events published via the event bus (`_publish_event` precedent), scoped to the **parent** conversation/user so the card renders in the parent chat:

- `subagent.started` → `{parent_conversation_id, subagent_id, agent_type, prompt_summary, visible_to}`
- `subagent.progress` → `{parent_conversation_id, subagent_id, round, note?, visible_to}` (per round)
- `subagent.completed` → `{parent_conversation_id, subagent_id, rounds, tokens_in, tokens_out, cost_usd, visible_to}`
- `subagent.failed` → `{parent_conversation_id, subagent_id, reason, visible_to}`

Frontend: a `<SubagentCard>` rendered in the chat stream, subscribed to these events (a new handler alongside the existing `chat.stream.*` handlers in the WS layer; per the frontend-extension rules this is core chat infrastructure, so it lives in core, not a plugin). v1 card = type + status + elapsed + final token/cost summary; streaming the subagent's inner steps is a follow-up (it can later subscribe to the subagent conversation's own stream).

**Open implementation question:** exact event→connection scoping (reuse the `visible_to` mechanism the `chat.stream.*` events already use; confirm the user/connection targeting in the WS bridge during planning).

### 6.6 `deep-research` type, profile, and model

- **Seeded "Deep Research" AI profile** (added to the built-in-profile seed in `_load_profiles`, or seeded by `SubagentService` on start): `backend="openrouter"`, `model="alibaba/tongyi-deepresearch-30b-a3b"`, `tool_mode="include"`, `tools=["web_search", "fetch_url"]`. If the profile's model/backend is unavailable, the engine falls back to the default profile/model (the type still works on any capable model).
- **openrouter catalog entry** for `alibaba/tongyi-deepresearch-30b-a3b` so it's selectable in the model picker and the profile resolves it.
- **`deep-research` agent type:** research preamble + a research system prompt (plan → search → read → reflect → synthesize a **cited** markdown report), `profile_name="deep-research"`, web-tools-only policy, `max_rounds` ~ depth tier.
- **Dependency:** requires a web-search backend (e.g. Tavily) enabled. If none is enabled, `deep_research`/the type returns a clear, actionable error rather than failing opaquely.

## 7. Data flow (one `deep_research` spawn)

1. Chat AI calls `deep_research(query="…")` (or the user types `/research …`).
2. `SubagentService.spawn("deep-research", query, caller_ctx)`: resolve type → build prompt (preamble + research prompt) → resolve headless tools (web tools only) → emit `subagent.started`.
3. Drive `AIService.chat` on a fresh ephemeral conversation with the "Deep Research" profile (Tongyi) and `max_tool_rounds`. The model iterates: `web_search` → `fetch_url` → reflect → … each round emits `subagent.progress`.
4. On END_TURN/budget, the model's final message is a cited markdown report. Emit `subagent.completed` with usage/cost.
5. Return the report text as the tool result. The parent chat turn incorporates/relays it; the `/research` path returns it directly.

## 8. Configuration

`SubagentService` is `Configurable` (full protocol: `config_namespace`, `config_category`, `config_params`, `on_config_changed`) and toggleable:

- `enabled` (BOOLEAN, default **True**) — operator can disable subagent spawning entirely.
- `subagent_preamble` (`ai_prompt=True`, multiline) — the shared headless preamble.
- `<type>_system_prompt` (`ai_prompt=True`, multiline) per built-in type — default = the type's bundled prompt. Read from cached `self._..._prompt` in `on_config_changed`, never from the `_DEFAULT_*` constant (per the AI-prompt rule).
- `default_max_rounds`, `default_max_wall_clock_s` — budget defaults (types may override).
- `model_profile` per type is governed by the seeded profile (editable in the Profiles UI), not duplicated here.

All non-trivial prompts are `ConfigParam(ai_prompt=True)` with the bundled string as `default` — no hardcoded prompts at call sites.

## 9. Error handling

- **Budget exhaustion** (`MAX_ROUNDS`/`WALL_CLOCK`/`TOKEN_BUDGET`): return the best partial result with a clear note ("stopped at the round/time budget") rather than erroring — the parent still gets usable output.
- **No web-search backend** (deep-research): clear, actionable tool error.
- **Model/profile unavailable:** fall back to the default profile; note the fallback.
- **Subagent run failure:** caught; `subagent.failed` emitted; a concise failure string returned as the tool result (the parent decides how to proceed). A subagent never crashes the parent turn.
- **Cost cap:** per-spawn budget plus an aggregate per-turn spawn cap (prevent a runaway parent fanning out unboundedly). Configurable.

## 10. Security / RBAC / isolation

- The subagent **inherits the caller's `UserContext`**; tool RBAC applies to the subagent exactly as to the caller (it can't do anything the caller couldn't).
- `spawn_agent` carries a `required_role`; per-type policies can tighten it.
- Headless gating guarantees no user-interaction tools and no nesting.
- Multi-user isolation: per-spawn state is request-scoped (passed as args / locals), never stored on the singleton service (`ContextVar`/locals per the isolation rules). Parallel spawns from concurrent users don't share mutable state.

## 11. Testing strategy

Unit tests with a mocked AI backend + fake tool providers (deterministic, no network):

- **Engine:** fresh context (parent history never leaks into the subagent message list); headless gating (interactive-flagged tools + `spawn_agent` excluded); per-type tool narrowing; budget termination (rounds/wall-clock) returns partial with the right note; final-message-as-result.
- **Tool layer:** `spawn_agent` enum/descriptions built from the registry; `deep_research` sugar routes to the deep-research type; `ToolDefinition.interactive` filtering.
- **Prompt config:** preamble + type prompt assembled from cached config values (not `_DEFAULT_*`); `on_config_changed` updates them.
- **Events:** `subagent.started/progress/completed/failed` emitted with correct parent scoping (fake event bus).
- **deep-research:** "no web-search backend" degradation; profile fallback when the Tongyi model is unavailable; report/citation passthrough.
- **Isolation:** two concurrent spawns don't cross state.

Frontend: a component test for `<SubagentCard>` state transitions (started → progress → completed/failed).

## 12. Build order (each a testable unit)

1. **Engine core** — `AgentType` + registry + `SubagentService.spawn(...)` driving `AIService.chat` on a fresh ephemeral context with budget; `general-purpose` built-in. `ToolDefinition.interactive` flag + headless gating. (No tool, no UI yet — tested directly.)
2. **Spawn tool** — `spawn_agent` exposed to chat; enum/descriptions from the registry; no-nesting exclusion.
3. **Live UI** — `subagent.*` events + `<SubagentCard>` in the chat stream.
4. **deep-research** — the type + seeded Tongyi "Deep Research" profile + openrouter catalog entry + `deep_research`/`/research` sugar + web-backend dependency handling.

## 13. Out of scope / future (Phase 2+)

- Reposition the durable `AgentService` as opt-in **autonomous agents** built on this engine (rename, toggle, re-base on the engine) — its own spec.
- User-defined agent types (editable entities / markdown).
- Asynchronous/background subagent runs with notification delivery.
- Richer research tools (scholar, code interpreter, file parsing); self-hosting Tongyi via Ollama/vLLM.
- Streaming the subagent's inner steps into its card.

## 14. Architecture-rules compliance

- **Core service**, composing capabilities via the resolver (AI, web tools, event bus) — no concrete cross-imports; uses `@runtime_checkable` protocols.
- **AI-backend visibility:** the engine never names a backend/model; the seeded profile does.
- **Configurable prompts:** preamble + type prompts are `ConfigParam(ai_prompt=True)`, read from cache.
- **RBAC:** `spawn_agent` declares `required_role`; subagent inherits caller identity.
- **Multi-user isolation:** request-scoped state only.
- **Docs:** add "subagent" to the Core glossary; record an ADR for the engine/layering decision; update root `README.md` / `CLAUDE.md` if the default-on capability or tool surface warrants it.

## 15. Open questions to resolve in planning

1. Ephemeral conversation handling (persist-hidden vs. a leaner `run_agentic` entrypoint) — §6.3. **Default: reuse `chat` with a hidden/ephemeral conversation.**
2. Exact `subagent.*` event→connection scoping in the WS bridge (reuse `visible_to`) — §6.5.
3. Whether `general-purpose`'s toolset is "all minus interactive/spawn" or a curated subset — **default: all minus interactive/spawn**.
4. Per-turn aggregate spawn/cost cap value — §9.
