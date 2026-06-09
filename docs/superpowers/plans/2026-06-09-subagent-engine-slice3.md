# Subagent Engine — Slice 3 (Live UI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface running subagents live in the chat UI — emit `chat.stream.subagent_*` lifecycle events from the engine, and render an active-subagent card in the chat stream. Also stand up the repo's first frontend test harness (vitest + React Testing Library).

**Architecture:** `SubagentService.spawn()` publishes `chat.stream.subagent_started/_completed/_failed` events (parent `conversation_id` from the chat-turn ContextVar, `visible_to=[caller]`) through the same event-bus→WS bridge `chat.stream.*` already uses — so existing WS `visible_to` scoping applies for free. On the frontend, a testable `useActiveSubagents(conversationId)` hook subscribes to those events and returns the live list; a presentational `<SubagentCard>` renders each (matching the `ThinkingCard` live-status pattern); `ChatPage` calls the hook and renders the cards. Vitest + RTL provide automated coverage for the hook and card.

**Tech Stack:** Backend: Python 3.12, pytest. Frontend: React 19 + Vite 8 + TS 6; new test deps vitest 4 + @testing-library/react 16 + jsdom.

**Reference spec:** `docs/superpowers/specs/2026-06-08-subagent-engine-design.md` §6.5. Builds on slices 1–2 (on `main`): `SubagentService` (`spawn`, `execute_tool`), the `spawn_agent` tool.

**Branch:** create `feat/subagent-slice3` off `main` before starting.

---

## File Structure

- **Modify** `src/gilbert/core/services/subagent.py` — store the resolver in `start()`; add `_publish_event`; emit lifecycle events around the `chat` call in `spawn()`.
- **Modify** `tests/unit/test_subagent_service.py` — event-emission tests (fake event bus + ContextVars).
- **Create** `frontend/vitest.config.ts` — vitest config (jsdom, `@` alias, setup file).
- **Create** `frontend/src/test/setup.ts` — RTL/jest-dom setup.
- **Modify** root `package.json` + `frontend/package.json` — add test devDeps + a `test` script.
- **Modify** `frontend/src/types/events.ts` — add `Subagent*` event payload types.
- **Create** `frontend/src/hooks/useActiveSubagents.ts` + `frontend/src/hooks/useActiveSubagents.test.tsx`.
- **Create** `frontend/src/components/chat/SubagentCard.tsx` + `frontend/src/components/chat/SubagentCard.test.tsx`.
- **Modify** `frontend/src/components/chat/ChatPage.tsx` — call the hook + render cards (minimal wiring).

Out of scope (later/never): per-round `subagent_progress` streaming into the card (v1 is started→done/failed); shared-room audience (v1 scopes to the caller); the `deep-research` type (slice 4).

---

## Task 0: Branch

- [ ] **Step 1: Create the feature branch**

```bash
cd /home/assistant/gilbert
git checkout main
git checkout -b feat/subagent-slice3
git rev-parse --abbrev-ref HEAD
```
Expected: `feat/subagent-slice3`.

---

## Task 1: Backend — emit `chat.stream.subagent_*` events

**Files:**
- Modify: `src/gilbert/core/services/subagent.py`
- Test: `tests/unit/test_subagent_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_subagent_service.py`:

```python
class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class _FakeEventBusProvider:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> _FakeBus:
        return self._bus


async def _started_with_bus(text: str = "done"):
    bus = _FakeBus()
    svc = SubagentService()
    fake = _FakeAI(text)
    await svc.start(_resolver(ai_chat=fake, event_bus=_FakeEventBusProvider(bus)))
    return svc, fake, bus


@pytest.mark.asyncio
async def test_spawn_emits_started_and_completed_events() -> None:
    from gilbert.interfaces.context import (
        _current_conversation_id,
        _current_user,
    )

    svc, _fake, bus = await _started_with_bus()
    caller = UserContext(user_id="u3", email="u3@x.com", display_name="U3")
    ut = _current_user.set(caller)
    ct = _current_conversation_id.set("conv-parent")
    try:
        await svc.spawn("general-purpose", "research X")
    finally:
        _current_user.reset(ut)
        _current_conversation_id.reset(ct)

    types = [e.event_type for e in bus.events]
    assert "chat.stream.subagent_started" in types
    assert "chat.stream.subagent_completed" in types
    started = next(e for e in bus.events if e.event_type == "chat.stream.subagent_started")
    # Routes to the PARENT conversation, visible only to the caller.
    assert started.data["conversation_id"] == "conv-parent"
    assert started.data["agent_type"] == "general-purpose"
    assert started.data["visible_to"] == ["u3"]
    assert "subagent_id" in started.data
    completed = next(e for e in bus.events if e.event_type == "chat.stream.subagent_completed")
    assert completed.data["subagent_id"] == started.data["subagent_id"]


@pytest.mark.asyncio
async def test_spawn_emits_failed_event_on_error() -> None:
    bus = _FakeBus()
    svc = SubagentService()

    class _BoomAI(_FakeAI):
        async def chat(self, *a: Any, **k: Any):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    await svc.start(_resolver(ai_chat=_BoomAI(), event_bus=_FakeEventBusProvider(bus)))
    with pytest.raises(RuntimeError, match="boom"):
        await svc.spawn("general-purpose", "task")
    types = [e.event_type for e in bus.events]
    assert "chat.stream.subagent_started" in types
    assert "chat.stream.subagent_failed" in types


@pytest.mark.asyncio
async def test_spawn_without_event_bus_still_works() -> None:
    # Events are best-effort: no event_bus capability -> spawn still returns.
    svc, _fake = await _started("ok")  # slice-1 helper, no bus
    assert await svc.spawn("general-purpose", "task") == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_subagent_service.py -k "emits_started or emits_failed or without_event_bus" -q`
Expected: FAIL — no events are published (the service doesn't emit yet); `chat.stream.subagent_started` not in the bus.

- [ ] **Step 3: Implement event emission**

In `src/gilbert/core/services/subagent.py`:

(a) Add imports at the top:

```python
import uuid

from gilbert.interfaces.context import (
    get_current_conversation_id,
    get_current_user,
)
```

(b) In `__init__`, add a resolver slot:

```python
        self._resolver: ServiceResolver | None = None
```

(c) In `start()`, store the resolver (keep the existing ai_chat binding):

```python
    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        ai = resolver.require_capability("ai_chat")
        if not isinstance(ai, AIProvider):
            raise RuntimeError("ai_chat capability does not implement AIProvider")
        self._ai = ai
        logger.info("Subagent service started")
```

(d) Add a `_publish_event` helper (mirrors `AIService._publish_event`; best-effort) — place it just before `spawn`:

```python
    async def _publish_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort publish to the event bus for live UI. No-op without one."""
        if self._resolver is None:
            return
        bus_svc = self._resolver.get_capability("event_bus")
        if bus_svc is None:
            return
        from gilbert.interfaces.events import Event, EventBusProvider

        if isinstance(bus_svc, EventBusProvider):
            await bus_svc.bus.publish(Event(event_type=event_type, data=data, source="subagent"))

    def _event_routing(self) -> dict[str, Any]:
        """Parent conversation + audience for subagent lifecycle events.

        Read from the chat-turn ContextVars: a subagent runs inside the
        spawning tool call, so these point at the PARENT chat. ``visible_to``
        scopes the event to the caller (the WS bridge applies it for
        ``chat.stream.*`` events). Both may be absent (direct, non-chat call)
        — then the event simply isn't routed to any conversation/user.
        """
        user = get_current_user()
        return {
            "conversation_id": get_current_conversation_id(),
            "visible_to": [user.user_id] if user.user_id else None,
        }
```

(e) Wrap the `chat` call in `spawn()` with start/completed/failed emission. Replace the body from `type_prompt = ...` through `return result.response_text` with:

```python
        type_prompt = self._type_prompts.get(agent.id, agent.system_prompt)
        system_prompt = f"{self._preamble}\n\n{type_prompt}"

        subagent_id = uuid.uuid4().hex
        routing = self._event_routing()
        await self._publish_event(
            "chat.stream.subagent_started",
            {**routing, "subagent_id": subagent_id, "agent_type": agent.id},
        )
        try:
            result = await self._ai.chat(
                user_message=prompt,
                conversation_id=None,
                user_ctx=user_ctx,
                system_prompt=system_prompt,
                ai_call=f"subagent.{agent.id}",
                ai_profile=agent.profile_name,
                max_tool_rounds=agent.max_rounds,
                headless=True,
            )
        except Exception as exc:
            await self._publish_event(
                "chat.stream.subagent_failed",
                {**routing, "subagent_id": subagent_id, "agent_type": agent.id,
                 "reason": str(exc)},
            )
            raise
        await self._publish_event(
            "chat.stream.subagent_completed",
            {**routing, "subagent_id": subagent_id, "agent_type": agent.id},
        )
        return result.response_text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_subagent_service.py -q`
Expected: PASS (all, including the three new event tests and the slice-1/2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
git commit -m "subagent: emit chat.stream.subagent_* lifecycle events for the live UI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Frontend test harness (vitest + React Testing Library)

**Files:**
- Modify: `package.json` (root) — test devDeps + script
- Modify: `frontend/package.json` — test devDeps + script
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/test/smoke.test.ts`

- [ ] **Step 1: Ensure the workspace is installed**

Run (from repo root — installs the npm workspace; may take a few minutes):
```bash
cd /home/assistant/gilbert
npm install
```
Expected: completes without error; `frontend/node_modules` (or root `node_modules`) populated. If it fails on network/registry, STOP and report BLOCKED.

- [ ] **Step 2: Add the test dependencies**

Run (root, targeting the frontend workspace):
```bash
cd /home/assistant/gilbert
npm install -D -w frontend vitest@^4 jsdom@^25 @testing-library/react@^16 @testing-library/jest-dom@^6 @testing-library/dom@^10
```
Expected: the five packages are added to `frontend/package.json` devDependencies and installed. If a version is unresolvable, let npm pick the latest compatible (drop the `@^N`) and note it.

- [ ] **Step 3: Add the `test` script to `frontend/package.json`**

In `frontend/package.json` `"scripts"`, add:
```json
    "test": "vitest run",
    "test:watch": "vitest",
```

- [ ] **Step 4: Create the setup file**

Create `frontend/src/test/setup.ts`:
```ts
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
```

- [ ] **Step 5: Create the vitest config**

Create `frontend/vitest.config.ts`:
```ts
import path from "path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Standalone test config — deliberately NOT importing vite.config.ts so the
// PWA/service-worker plugin and dev proxy don't load under jsdom.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
```

- [ ] **Step 6: Create a smoke test**

Create `frontend/src/test/smoke.test.ts`:
```ts
import { describe, expect, it } from "vitest";

describe("test harness", () => {
  it("runs", () => {
    expect(1 + 1).toBe(2);
  });
});
```

- [ ] **Step 7: Run the harness**

Run:
```bash
cd /home/assistant/gilbert/frontend
npm run test
```
Expected: vitest runs and the smoke test passes (1 passed). If vitest can't find the config, confirm `vitest.config.ts` is at `frontend/` and rerun.

- [ ] **Step 8: Commit**

```bash
cd /home/assistant/gilbert
git add package.json package-lock.json frontend/package.json frontend/vitest.config.ts frontend/src/test/setup.ts frontend/src/test/smoke.test.ts
git commit -m "frontend: add vitest + React Testing Library harness

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `useActiveSubagents` hook + `<SubagentCard>`

**Files:**
- Modify: `frontend/src/types/events.ts`
- Create: `frontend/src/hooks/useActiveSubagents.ts` + `.test.tsx`
- Create: `frontend/src/components/chat/SubagentCard.tsx` + `.test.tsx`

- [ ] **Step 1: Add event payload types**

Append to `frontend/src/types/events.ts`:
```ts
/** Lifecycle status of a subagent spawned within a chat turn. */
export type SubagentStatus = "running" | "completed" | "failed";

/** Data payload shared by the chat.stream.subagent_* events. */
export interface SubagentEventData {
  conversation_id: string | null;
  subagent_id: string;
  agent_type: string;
  reason?: string;
}

/** A subagent tracked live in the UI for the active conversation. */
export interface ActiveSubagent {
  subagent_id: string;
  agent_type: string;
  status: SubagentStatus;
  reason?: string;
}
```

- [ ] **Step 2: Write the failing hook test**

Create `frontend/src/hooks/useActiveSubagents.test.tsx`:
```tsx
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { GilbertEvent } from "@/types/events";
import { useActiveSubagents } from "./useActiveSubagents";

// Capture event-bus handlers so tests can fire events synchronously.
const handlers = new Map<string, (e: GilbertEvent) => void>();
vi.mock("@/hooks/useEventBus", () => ({
  useEventBus: (type: string, handler: (e: GilbertEvent) => void) => {
    handlers.set(type, handler);
  },
}));

function fire(event_type: string, data: Record<string, unknown>) {
  act(() => {
    handlers.get(event_type)?.({ event_type, data, source: "subagent", timestamp: "" });
  });
}

describe("useActiveSubagents", () => {
  it("tracks running subagents for the active conversation, ignores others", () => {
    const { result } = renderHook(() => useActiveSubagents("conv-1"));
    expect(result.current).toEqual([]);

    fire("chat.stream.subagent_started", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    fire("chat.stream.subagent_started", {
      conversation_id: "other",
      subagent_id: "b1",
      agent_type: "general-purpose",
    });

    expect(result.current).toHaveLength(1);
    expect(result.current[0]).toMatchObject({
      subagent_id: "a1",
      agent_type: "general-purpose",
      status: "running",
    });
  });

  it("marks a subagent completed", () => {
    const { result } = renderHook(() => useActiveSubagents("conv-1"));
    fire("chat.stream.subagent_started", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    fire("chat.stream.subagent_completed", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    expect(result.current[0].status).toBe("completed");
  });

  it("marks a subagent failed with a reason", () => {
    const { result } = renderHook(() => useActiveSubagents("conv-1"));
    fire("chat.stream.subagent_started", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    fire("chat.stream.subagent_failed", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
      reason: "boom",
    });
    expect(result.current[0].status).toBe("failed");
    expect(result.current[0].reason).toBe("boom");
  });
});
```

- [ ] **Step 3: Run the hook test to verify it fails**

Run: `cd /home/assistant/gilbert/frontend && npm run test -- useActiveSubagents`
Expected: FAIL — `useActiveSubagents` module does not exist.

- [ ] **Step 4: Implement the hook**

Create `frontend/src/hooks/useActiveSubagents.ts`:
```ts
import { useCallback, useState } from "react";
import { useEventBus } from "@/hooks/useEventBus";
import type { ActiveSubagent, GilbertEvent, SubagentStatus } from "@/types/events";

/**
 * Tracks subagents spawned during the active conversation's turns, live.
 * Subscribes to the chat.stream.subagent_* events and returns the current
 * list (running first by arrival). Events for other conversations are ignored.
 */
export function useActiveSubagents(activeConversationId: string | null): ActiveSubagent[] {
  const [byId, setById] = useState<Record<string, ActiveSubagent>>({});

  const upsert = useCallback(
    (status: SubagentStatus) => (event: GilbertEvent) => {
      const d = event.data as Record<string, unknown>;
      if (d.conversation_id !== activeConversationId) return;
      const id = String(d.subagent_id || "");
      if (!id) return;
      setById((prev) => ({
        ...prev,
        [id]: {
          subagent_id: id,
          agent_type: String(d.agent_type || "agent"),
          status,
          reason: typeof d.reason === "string" ? d.reason : prev[id]?.reason,
        },
      }));
    },
    [activeConversationId],
  );

  useEventBus("chat.stream.subagent_started", upsert("running"));
  useEventBus("chat.stream.subagent_completed", upsert("completed"));
  useEventBus("chat.stream.subagent_failed", upsert("failed"));

  return Object.values(byId);
}
```

- [ ] **Step 5: Run the hook test to verify it passes**

Run: `cd /home/assistant/gilbert/frontend && npm run test -- useActiveSubagents`
Expected: PASS (3 passed).

- [ ] **Step 6: Write the failing card test**

Create `frontend/src/components/chat/SubagentCard.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ActiveSubagent } from "@/types/events";
import { SubagentCard } from "./SubagentCard";

const base: ActiveSubagent = {
  subagent_id: "a1",
  agent_type: "general-purpose",
  status: "running",
};

describe("SubagentCard", () => {
  it("shows the agent type and a running state", () => {
    render(<SubagentCard subagent={base} />);
    expect(screen.getByText(/general-purpose/i)).toBeInTheDocument();
    expect(screen.getByText(/running/i)).toBeInTheDocument();
  });

  it("shows completed state", () => {
    render(<SubagentCard subagent={{ ...base, status: "completed" }} />);
    expect(screen.getByText(/done|completed/i)).toBeInTheDocument();
  });

  it("shows the failure reason", () => {
    render(<SubagentCard subagent={{ ...base, status: "failed", reason: "boom" }} />);
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
    expect(screen.getByText(/boom/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 7: Run the card test to verify it fails**

Run: `cd /home/assistant/gilbert/frontend && npm run test -- SubagentCard`
Expected: FAIL — `SubagentCard` module does not exist.

- [ ] **Step 8: Implement the card**

Create `frontend/src/components/chat/SubagentCard.tsx` (mirrors the `ThinkingCard` live-status look in `TurnBubble.tsx` — pulsing/dashed while running):
```tsx
import { CheckIcon, LoaderIcon, XCircleIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ActiveSubagent } from "@/types/events";

const STATUS_LABEL: Record<ActiveSubagent["status"], string> = {
  running: "Running",
  completed: "Done",
  failed: "Failed",
};

export function SubagentCard({ subagent }: { subagent: ActiveSubagent }) {
  const running = subagent.status === "running";
  const failed = subagent.status === "failed";
  const Icon = running ? LoaderIcon : failed ? XCircleIcon : CheckIcon;
  return (
    <div
      className={cn(
        "w-full max-w-2xl rounded-md border border-border bg-card/40 my-2 px-3 py-1.5",
        "flex items-center gap-2 text-xs",
        running && "border-dashed border-(--signal)/40 animate-pulse",
        failed && "border-rose-500/40",
      )}
    >
      <Icon className={cn("size-3 shrink-0", running && "animate-spin")} />
      <span className="font-medium">Subagent: {subagent.agent_type}</span>
      <span className="text-muted-foreground">— {STATUS_LABEL[subagent.status]}</span>
      {failed && subagent.reason ? (
        <span className="text-rose-400 truncate">— {subagent.reason}</span>
      ) : null}
    </div>
  );
}
```

(If `@/lib/utils` `cn` is not the correct import path in this repo, use the same import the existing `TurnBubble.tsx` uses for `cn`.)

- [ ] **Step 9: Run the card test to verify it passes**

Run: `cd /home/assistant/gilbert/frontend && npm run test -- SubagentCard`
Expected: PASS (3 passed).

- [ ] **Step 10: Commit**

```bash
cd /home/assistant/gilbert
git add frontend/src/types/events.ts frontend/src/hooks/useActiveSubagents.ts frontend/src/hooks/useActiveSubagents.test.tsx frontend/src/components/chat/SubagentCard.tsx frontend/src/components/chat/SubagentCard.test.tsx
git commit -m "frontend: useActiveSubagents hook + SubagentCard (tested)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the cards into ChatPage

**Files:**
- Modify: `frontend/src/components/chat/ChatPage.tsx`

This is minimal, pattern-following wiring. The component already imports `useEventBus` and tracks `activeConvId`.

- [ ] **Step 1: Import the hook and card**

Add to the imports at the top of `ChatPage.tsx`:
```tsx
import { useActiveSubagents } from "@/hooks/useActiveSubagents";
import { SubagentCard } from "@/components/chat/SubagentCard";
```

- [ ] **Step 2: Call the hook**

In the `ChatPage` component body, near where `activeConvId` is declared (around line 67), add:
```tsx
  const activeSubagents = useActiveSubagents(activeConvId);
```

- [ ] **Step 3: Render the cards**

Find where the message list / streaming turn is rendered in `ChatPage`'s returned JSX (search for the `<MessageList` element, or the container that holds `turns`). Immediately below that element (still inside the scrollable chat column, so the cards appear at the bottom of the stream under the in-flight turn), render:
```tsx
          {activeSubagents.length > 0 && (
            <div className="px-4">
              {activeSubagents.map((sa) => (
                <SubagentCard key={sa.subagent_id} subagent={sa} />
              ))}
            </div>
          )}
```
Match the surrounding indentation/container classes (use the same horizontal padding the message list uses, e.g. `px-4`, so the cards align with messages). Keep it inside the same scroll container as the turns.

- [ ] **Step 4: Type-check the frontend**

Run:
```bash
cd /home/assistant/gilbert/frontend
npm run typecheck
```
Expected: no type errors. Fix any introduced by the new imports/usage (e.g. an unused import) before continuing.

- [ ] **Step 5: Run the full frontend test suite**

Run: `cd /home/assistant/gilbert/frontend && npm run test`
Expected: PASS (smoke + hook + card tests all green).

- [ ] **Step 6: Commit**

```bash
cd /home/assistant/gilbert
git add frontend/src/components/chat/ChatPage.tsx
git commit -m "frontend: render live SubagentCards in the chat stream

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Verification

**Files:** none (verification only)

- [ ] **Step 1: Backend lint/type/tests**

Run:
```bash
cd /home/assistant/gilbert
uv run ruff check src/gilbert/core/services/subagent.py tests/unit/test_subagent_service.py
uv run mypy src/gilbert/core/services/subagent.py
uv run pytest tests/unit/ -q
```
Expected: ruff clean on these files; mypy `Success`; full unit suite green.

- [ ] **Step 2: Frontend type + lint + test**

Run:
```bash
cd /home/assistant/gilbert/frontend
npm run typecheck
npm run lint 2>&1 | tail -20 || true
npm run test
```
Expected: typecheck clean; tests pass. (If `npm run lint` reports PRE-EXISTING issues in files you didn't touch, leave them; fix only your new files.)

- [ ] **Step 3: Frontend production build (catches integration errors vitest/jsdom miss)**

Run: `cd /home/assistant/gilbert/frontend && npm run build 2>&1 | tail -15`
Expected: `tsc -b && vite build` completes without error. (This compiles the whole SPA incl. the ChatPage wiring — the closest automated check that the UI integration is sound.)

- [ ] **Step 4: Commit any fixups**

```bash
cd /home/assistant/gilbert
git add -A
git commit -m "subagent slice3: verification fixups" || echo "nothing to commit"
```

---

## Self-review notes (author check)

- **Spec coverage (§6.5):** lifecycle events `subagent.started/completed/failed` emitted (Task 1) — named under `chat.stream.*` so the existing WS `visible_to` scoping applies; scoped to the parent `conversation_id` + caller; an active-subagent card rendered in the chat stream (Tasks 3–4); minimal v1 card (running → done/failed). ✓
- **Deferred (called out):** per-round `subagent_progress` into the card; shared-room audience (v1 = caller only); the `deep-research` type (slice 4).
- **Testability:** event-driven logic lives in the `useActiveSubagents` hook + the `SubagentCard` (both vitest-tested); ChatPage wiring is a hook call + a `.map()` render (verified by `typecheck` + `vite build`). Backend emission is pytest-tested with a fake bus + ContextVars.
- **Type/name consistency:** event names `chat.stream.subagent_started/_completed/_failed` are identical across backend emission (Task 1), the hook's `useEventBus` subscriptions (Task 3), and the tests. Payload keys (`conversation_id`, `subagent_id`, `agent_type`, `reason`, `visible_to`) match between the backend dict and the `SubagentEventData`/`ActiveSubagent` types.
- **Known limitation (acknowledged):** the React rendering is verified by vitest (jsdom) + `tsc` + `vite build`, not by running the live SPA. A manual check against a running Gilbert instance is recommended after merge but is not part of automated CI.
- **No placeholders:** every code step has complete code; every run step has an exact command + expected result. The one judgment call left to the implementer (exact JSX insertion point + the `cn` import path in ChatPage) is explicitly flagged with how to resolve it from the surrounding code.
