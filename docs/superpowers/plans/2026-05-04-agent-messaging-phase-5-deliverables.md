# Agent Messaging — Phase 5: Deliverables + Dependency Wake-up Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** First-class `Deliverable` and `GoalDependency` entities. Deliverables transition DRAFT → READY → OBSOLETE. When a Deliverable flips to READY, every `GoalDependency` row matching `(source_goal_id, name)` is satisfied; assignees on the dependent goal (DRIVER + COLLABORATOR; reviewers excluded) wake via `_signal_agent` with `signal_kind="deliverable_ready"`. Five new agent tools + matching WS RPCs + a cross-goal file-access helper. The Phase 4 right-rail placeholders are replaced with real `<DeliverablesPanel>` and `<DependenciesPanel>` components. **Acceptance:** A produces `spec` deliverable → finalize → B's drivers wake → B can read the underlying file via the cross-goal grant.

**Architecture:** Entities + methods on `AgentService` (already houses Goal). New collections: `goal_deliverables`, `goal_dependencies`. A finalized Deliverable triggers an `on_deliverable_finalized` propagation: query unsatisfied `GoalDependency` rows whose `source_goal_id` matches and whose `required_deliverable_name` matches; mark each `satisfied_at`; signal each non-REVIEWER assignee on the dependent goal. Cross-goal file access: a new `WorkspaceProvider.resolve_deliverable_for_dependent(file_id, viewing_agent_id, viewing_goal_id)` returns `(path, error)` iff the file is referenced by a READY deliverable on a goal that `viewing_goal_id` depends on. The existing `read_workspace_file` tool gains optional `goal_id=` arg that consults the new resolver before falling back to per-conv ACL.

**Out of scope:**
- Cross-user — Phase 6.
- Workspace cleanup on goal deletion.
- Run-cost rollup onto `Goal.lifetime_cost_usd` (still deferred from Phase 4).
- Auto-generation of deliverables from agent output (manual via `deliverable_create`).

---

## File Structure

**Modify:**
- `src/gilbert/interfaces/agent.py` — `Deliverable`, `GoalDependency`, `DeliverableState` enum. Extend `AgentProvider` protocol.
- `src/gilbert/interfaces/workspace.py` — add `resolve_deliverable_for_dependent` method to the protocol (default-implementations TBD via abstract).
- `src/gilbert/core/services/agent.py` — collections, CRUD, lifecycle, propagation, tools, WS RPCs.
- `src/gilbert/core/services/workspace.py` — implement `resolve_deliverable_for_dependent`.
- `src/gilbert/interfaces/acl.py` — add `"deliverables.": 100` (and confirm `goals.` covers `goals.dependencies.*`).
- `frontend/src/types/agent.ts` — Deliverable / GoalDependency / DeliverableState types.
- `frontend/src/api/goals.ts` — deliverables + dependencies hooks.
- `frontend/src/components/goals/WarRoomPage.tsx` — replace placeholders with real panels.
- `.claude/memory/memory-agent-service.md` — Phase 5 subsection.
- `tests/unit/test_agent_avatar_route.py` and `tests/unit/test_agent_entities.py` — extend the runtime-checkable fakes for new `AgentProvider` methods.

**Create:**
- `tests/unit/test_deliverables.py`
- `tests/unit/test_dependencies.py`
- `tests/unit/test_cross_goal_access.py`
- `frontend/src/components/goals/DeliverablesPanel.tsx`
- `frontend/src/components/goals/DependenciesPanel.tsx`

---

## Tasks

### Task 1: Entities

Add to `src/gilbert/interfaces/agent.py`:

```python
class DeliverableState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    OBSOLETE = "obsolete"


@dataclass
class Deliverable:
    id: str
    goal_id: str
    name: str                # logical name dependents reference
    kind: str                # "spec" | "code" | "report" | "image" | …
    state: DeliverableState
    produced_by_agent_id: str
    content_ref: str         # "workspace_file:<id>" | inline text | URL
    created_at: datetime
    finalized_at: datetime | None


@dataclass
class GoalDependency:
    id: str
    dependent_goal_id: str
    source_goal_id: str
    required_deliverable_name: str
    satisfied_at: datetime | None
```

Extend `AgentProvider` with method stubs.

Update fakes in `test_agent_avatar_route.py` and `test_agent_entities.py` to satisfy the runtime-checkable protocol. Add stubs for: `create_deliverable`, `get_deliverable`, `list_deliverables`, `finalize_deliverable`, `supersede_deliverable`, `add_goal_dependency`, `remove_goal_dependency`, `list_goal_dependencies`.

Tests in NEW `tests/unit/test_deliverables.py`:
- Round-trip Deliverable + GoalDependency dataclasses.
- DeliverableState enum values.

Commit: `deliverables: entity model + protocol additions`

---

### Task 2: AgentService CRUD + lifecycle + propagation

Add to `src/gilbert/core/services/agent.py`:

Collections: `_DELIVERABLES_COLLECTION = "goal_deliverables"`, `_DEPENDENCIES_COLLECTION = "goal_dependencies"`.

Serializers `_deliverable_to_dict / _deliverable_from_dict / _dependency_to_dict / _dependency_from_dict`.

Methods:

```python
async def create_deliverable(
    self,
    *,
    goal_id: str,
    name: str,
    kind: str,
    produced_by_agent_id: str,
    content_ref: str = "",
    state: DeliverableState = DeliverableState.DRAFT,
) -> Deliverable: ...

async def get_deliverable(self, deliverable_id: str) -> Deliverable | None: ...

async def list_deliverables(
    self,
    *,
    goal_id: str | None = None,
    state: DeliverableState | None = None,
) -> list[Deliverable]: ...

async def finalize_deliverable(self, deliverable_id: str) -> Deliverable: ...

async def supersede_deliverable(
    self,
    deliverable_id: str,
    *,
    new_content_ref: str,
    finalize: bool = False,
) -> tuple[Deliverable, Deliverable]: ...  # (obsolete_old, new_one)

async def add_goal_dependency(
    self,
    *,
    dependent_goal_id: str,
    source_goal_id: str,
    required_deliverable_name: str,
) -> GoalDependency: ...

async def remove_goal_dependency(self, dependency_id: str) -> None: ...

async def list_goal_dependencies(
    self,
    *,
    dependent_goal_id: str | None = None,
    source_goal_id: str | None = None,
    satisfied: bool | None = None,
) -> list[GoalDependency]: ...
```

Behavior:

- `create_deliverable` always starts DRAFT (caller can pass `state=READY` only via the supersede path).
- `finalize_deliverable`: refuses if state is OBSOLETE; sets state=READY + finalized_at; **enforces "single READY per (goal_id, name)"** by transitively superseding any prior READY deliverable with the same name on the same goal (mark them OBSOLETE in the same put-batch); fires `_on_deliverable_finalized`.
- `supersede_deliverable`: marks the old row OBSOLETE and creates a new DRAFT (or READY if `finalize=True`) with the SAME `name` on the same goal. Returns both rows.
- `add_goal_dependency`: idempotent on the (dependent, source, required_name) triple. If a matching READY deliverable already exists on `source_goal_id`, the new dependency is created with `satisfied_at` populated immediately AND fires the wake-up signal.
- `remove_goal_dependency`: deletes row.
- `list_goal_dependencies` filters: `dependent_goal_id`, `source_goal_id`, satisfied=True/False/None.

`_on_deliverable_finalized(d: Deliverable)`:

```python
deps = await self.list_goal_dependencies(
    source_goal_id=d.goal_id,
    satisfied=False,  # unsatisfied only
)
for dep in deps:
    if dep.required_deliverable_name != d.name:
        continue
    # Mark satisfied
    row = await self._storage.get(_DEPENDENCIES_COLLECTION, dep.id)
    row["satisfied_at"] = _now().isoformat()
    await self._storage.put(_DEPENDENCIES_COLLECTION, dep.id, row)

    # Signal non-REVIEWER assignees on the dependent goal.
    assignments = await self.list_assignments(
        goal_id=dep.dependent_goal_id, active_only=True,
    )
    for asgn in assignments:
        if asgn.role is AssignmentRole.REVIEWER:
            continue
        await self._signal_agent(
            agent_id=asgn.agent_id,
            signal_kind="deliverable_ready",
            body=f"Dependency satisfied: {d.name} from goal {dep.source_goal_id}",
            sender_kind="system",
            sender_id="",
            sender_name="system",
            metadata={
                "deliverable_id": d.id,
                "source_goal_id": d.goal_id,
                "dependent_goal_id": dep.dependent_goal_id,
            },
        )

await self._publish(
    "goal.deliverable.finalized",
    {"deliverable_id": d.id, "goal_id": d.goal_id, "name": d.name},
)
```

Update `goal_summary` (Phase 4) to compute `is_dependency_blocked`: True iff any `list_goal_dependencies(dependent_goal_id=goal_id, satisfied=False)` exists.

Tests in `tests/unit/test_deliverables.py`:
- `test_create_deliverable_starts_draft`
- `test_finalize_sets_ready_and_supersedes_prior`
- `test_supersede_creates_new_draft`
- `test_supersede_with_finalize_creates_ready`
- `test_finalize_obsolete_raises`

Tests in NEW `tests/unit/test_dependencies.py`:
- `test_add_dependency_idempotent`
- `test_finalize_satisfies_unsatisfied_deps`
- `test_finalize_signals_non_reviewer_assignees`
- `test_finalize_does_not_signal_reviewers`
- `test_add_dependency_immediately_satisfied_when_source_already_ready`
- `test_obsolete_does_not_satisfy`
- `test_goal_summary_reflects_dependency_blocked`

Commit: `deliverables: AgentService CRUD + propagation + summary blocked-flag`

---

### Task 3: Cross-goal file access (WorkspaceProvider)

Modify `src/gilbert/interfaces/workspace.py`: add to the `WorkspaceProvider` protocol:

```python
def resolve_deliverable_for_dependent(
    self,
    *,
    file_id: str,
    viewing_agent_id: str,
    viewing_goal_id: str,
) -> tuple[Path | None, str | None]:
    """Return the file path iff:
    - the file_id is referenced as a Deliverable.content_ref on a goal G; AND
    - viewing_goal_id has a registered GoalDependency on G; AND
    - the deliverable is currently READY.

    Returns ``(path, None)`` on success, ``(None, error_message)`` on
    rejection (including the deliverable being OBSOLETE).
    """
```

Implement in `src/gilbert/core/services/workspace.py`. Implementation:
- Look up the workspace_files row by `file_id` (existing query path).
- Find the deliverable that references `workspace_file:<file_id>` as its `content_ref`. If none, return `(None, "no deliverable references this file")`.
- Confirm `state == READY`. If OBSOLETE, return `(None, "deliverable is OBSOLETE")`.
- Confirm `viewing_goal_id` has a `GoalDependency(dependent_goal_id=viewing_goal_id, source_goal_id=deliverable.goal_id, required_deliverable_name=deliverable.name, satisfied_at IS NOT NULL)`. If missing, return `(None, "no dependency grants access")`.
- Resolve the file's on-disk path via existing storage entries; return `(path, None)`.

The workspace service may need a new helper `_get_deliverable_by_file_id` and `_get_dependency_match` — or it can call into AgentService through the capability registry. Pick the cleanest path. Recommended: have AgentService own the entity queries and expose them as small helpers; the workspace service consumes them via `agent` capability.

Tests in NEW `tests/unit/test_cross_goal_access.py`:
- `test_resolve_grants_access_when_dependency_satisfied`
- `test_resolve_blocks_when_dependency_missing`
- `test_resolve_blocks_when_obsolete`
- `test_resolve_blocks_unrelated_file`

Commit: `deliverables: cross-goal workspace file access`

---

### Task 4: Tools

Five new tools (slash_namespace="agents"):

- `deliverable_create(goal_id, name, kind, content_ref?)` — assignee-only; produced_by_agent_id = caller.
- `deliverable_finalize(id)` — producer OR DRIVER.
- `deliverable_supersede(id, new_content_ref, finalize?)` — producer OR DRIVER.
- `goal_add_dependency(goal_id, source_goal, name)` — DRIVER on `goal_id`.
- `goal_remove_dependency(dep_id)` — DRIVER on the dependent goal.

These are NOT core. Operators may pin them via `tools_allowed`.

Update `read_workspace_file` (find the existing tool — likely registered by the workspace service or `core/services/skill.py` or similar) to accept an optional `goal_id` parameter. When provided AND the standard per-conv ACL fails, fall back to `WorkspaceProvider.resolve_deliverable_for_dependent(file_id, viewing_agent_id=_agent_id, viewing_goal_id=goal_id)`. If that succeeds, return the file content; otherwise return the original ACL error.

Tests in `tests/unit/test_deliverables.py`:
- `test_deliverable_create_via_tool`
- `test_deliverable_finalize_producer_or_driver`
- `test_deliverable_supersede_via_tool`
- `test_goal_add_dependency_via_tool`
- `test_read_workspace_file_via_dependency_grant` (skip if `read_workspace_file` plumbing is too involved — fall back to a unit test on `resolve_deliverable_for_dependent`)

Commit: `deliverables: tools (create / finalize / supersede / add_dep / remove_dep)`

---

### Task 5: WS RPCs

Handlers (extend `AgentService.get_ws_handlers()`):

```
deliverables.list(goal_id?, state?)
deliverables.create(goal_id, name, kind, content_ref?)
deliverables.finalize(deliverable_id)
deliverables.supersede(deliverable_id, new_content_ref, finalize?)
goals.dependencies.list(dependent_goal_id?, source_goal_id?, satisfied?)
goals.dependencies.add(dependent_goal_id, source_goal_id, required_deliverable_name)
goals.dependencies.remove(dependency_id)
```

Per-user RBAC: caller must own the relevant goal(s).

Add `"deliverables.": 100` to `interfaces/acl.py`.

Tests in `tests/unit/test_dependencies.py`:
- `test_ws_deliverables_owner_only`
- `test_ws_dependencies_owner_only`

Commit: `deliverables: WS RPCs (deliverables.* + goals.dependencies.*)`

---

### Task 6: Frontend types + API + panels

**Modify `frontend/src/types/agent.ts`** — add Deliverable / GoalDependency / DeliverableState types.

**Modify `frontend/src/api/goals.ts`** (or sibling) — add hooks:
- `useDeliverables(goalId, state?)`
- `useCreateDeliverable`, `useFinalizeDeliverable`, `useSupersedeDeliverable`
- `useDependencies(dependentGoalId?, sourceGoalId?, satisfied?)`
- `useAddDependency`, `useRemoveDependency`

Each mutation invalidates relevant keys. Subscribe to `goal.deliverable.finalized` event in pages that show deliverables/deps.

**Create**:
- `frontend/src/components/goals/DeliverablesPanel.tsx` — list of deliverables for a goal (name, kind, state badge, producer agent name, created_at). Header has "+ New deliverable" dialog (asks for name, kind, content_ref). Per-row Finalize / Supersede buttons (visible to producer + DRIVER).
- `frontend/src/components/goals/DependenciesPanel.tsx` — two lists: outgoing (this goal depends on source goals — show satisfied checkmark + click-to-source) and incoming (other goals depend on this goal). "+ Add dependency" dialog (select source goal + required deliverable name).

**Modify `frontend/src/components/goals/WarRoomPage.tsx`** — replace right-rail placeholders with the real panels.

Commit: `deliverables (frontend): types + API + DeliverablesPanel + DependenciesPanel`

---

### Task 7: Memory + verification

Append "Phase 5 — Deliverables + dependency wake-up" subsection to `.claude/memory/memory-agent-service.md`.

Verification:
- `uv run pytest -x`
- `uv run ruff check src/ tests/unit/test_deliverables.py tests/unit/test_dependencies.py tests/unit/test_cross_goal_access.py`
- `uv run mypy src/gilbert/core/services/agent.py src/gilbert/core/services/workspace.py src/gilbert/interfaces/agent.py src/gilbert/interfaces/workspace.py`
- `tsc -b` clean.

---

## Test Strategy

| Category | Coverage |
|---|---|
| Entity round-trip | Deliverable + GoalDependency. |
| Lifecycle | create → finalize → supersede; OBSOLETE refuses finalize. |
| Single-READY invariant | finalizing a 2nd `same-name` row marks the previous READY one OBSOLETE in one transaction. |
| Propagation | Finalize fires non-REVIEWER assignee signals on dependent goals. REVIEWERs excluded. |
| Pre-satisfied | Adding a dependency where the source already has a READY matching deliverable: dep is created with `satisfied_at` populated, signal fires immediately. |
| Cross-goal access | resolver grants access only for READY, registered, same-named deliverables; OBSOLETE blocked; missing dependency edge blocked. |
| Tools RBAC | non-assignee `deliverable_create` blocked; non-DRIVER `goal_add_dependency` blocked; non-producer `deliverable_finalize` allowed iff DRIVER. |
| Goal summary | `is_dependency_blocked` reflects unsatisfied deps. |
| WS RPCs | Owner-only enforcement on each new handler. |
| Frontend tsc | Clean with new panels; WarRoomPage placeholders replaced. |

---

## Open Questions / Future

- **Workspace cleanup on goal deletion.** Goals are CANCELLED, not deleted, so deliverable files persist. A purge tool needs to consider whether dependents still reference the file. Phase 5+ polish.
- **Auto-creating deliverables from agent output.** The agent has to manually call `deliverable_create`. Could detect output files (e.g., a tool writes a workspace file marked as a deliverable) automatically. Not Phase 5.
- **Run-cost rollup onto goals.** Still deferred (Phase 4 open question).
- **`read_workspace_file` integration.** If updating that tool's signature is too disruptive (lots of plugins reference it), keep `resolve_deliverable_for_dependent` as a standalone helper and add a new small tool `read_dependency_file(file_id, goal_id)` instead. Make the call.
