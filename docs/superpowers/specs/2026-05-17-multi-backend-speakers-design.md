# Multi-Backend SpeakerService — Design

**Date:** 2026-05-17
**Branch:** `feature/browser_speaker_backend` (bundled into PR #16)
**Status:** Design approved; ready for implementation planning.

## Goal

Convert Gilbert's `SpeakerService` from "one backend chosen by config" to "N backends running simultaneously" so a single Gilbert install can drive Sonos speakers, the host's local audio, and per-user browser tabs concurrently. Downstream consumers (MusicService, doorbell, radio, scheduler, guess-that-song) are updated in the same cycle so the refactor lands without lingering single-backend assumptions.

## Decisions captured during brainstorming

| Decision | Choice |
|---|---|
| Backend cardinality | One instance per backend type; any combination active at once (mirrors `AIService` aggregator) |
| Scope | Speakers + every downstream consumer in one bundled PR |
| Grouping policy | Per-backend groups only — `group_speakers` rejects cross-backend; `play_on_speakers` silently fans out across backends (un-synchronized) |
| Default targets when unset | A designated `primary_backend` in config receives bare `announce()` / `play()` calls |
| Migration | One-shot boot-time DB migration namespaces stored alias rows and rewrites legacy `speaker.backend` config |

## Layer / pattern context

The new shape copies the established aggregator pattern (`TTSService`, `AIService`). One service, N backends loaded simultaneously, requests routed by parameter / config. Backends register through `Backend.__init_subclass__` and are discovered via `SpeakerBackend.registered_backends()`. The `validate-architecture` skill's category 4 documents this pattern; the user's `project_multi_backend_pattern` memory reinforces it.

---

## 1. Interface changes (`src/gilbert/interfaces/speaker.py`)

**Speaker IDs are namespaced as `<backend>:<native>` above the backend boundary.** `SpeakerService` strips the prefix on the way in to a backend method call and stamps it on the way out from `list_speakers()`. Backends never see the prefix.

**Dataclass additions:**

- `SpeakerInfo.backend_name: str = ""` — stamped by the service during list-merge so the UI can disambiguate name collisions ("Living Room · sonos" vs "Living Room · browser").
- `SpeakerGroup.backend_name: str = ""` — groups never span backends; stamping makes the rejection check mechanical.

**New helper:**

- `def split_speaker_id(sid: str) -> tuple[str, str]` — returns `(backend_name, native_id)`; raises `ValueError` on un-prefixed input so callers can't accidentally pass a bare ID.

**`SpeakerProvider` capability protocol (in `interfaces/speaker.py`) gains:**

- `def get_backend(name: str) -> SpeakerBackend | None`
- `@property backends -> Mapping[str, SpeakerBackend]`
- `async def resolve_names(names: list[str]) -> dict[str, str]` — returns `{name: namespaced_id}`. Used by MusicService and any consumer that needs the resolved IDs before dispatch (so it can group them by backend, validate compatibility, etc.) without taking on name-resolution itself.

**`SpeakerProvider.backend` (singular) is removed entirely.** No deprecated shim. All ~5–10 callsites are updated in the same PR.

**Backend protocols unchanged.** `SpeakerBackend`, `EventBusAwareSpeakerBackend`, `CachedSpeakerLister` keep their signatures. Existing Sonos / local / browser backends need no internal change — they continue to produce native IDs.

---

## 2. Service refactor (`src/gilbert/core/services/speaker.py`)

**State:**

```python
self._backends: dict[str, SpeakerBackend] = {}      # name → instance
self._primary_backend: str = ""                      # name of designated primary
self._users_svc: UserPrefReader | None = None
self._event_bus_provider: EventBusProvider | None = None
self._speaker_locks: dict[str, asyncio.Lock] = {}   # keyed by namespaced ID
```

**Lifecycle (`start()` + `on_config_changed()`):**

- Copy `AIService._reinit_backends` shape verbatim. Iterate `SpeakerBackend.registered_backends()`; instantiate any with `backends.<name>.enabled = True`; await each `initialize(per_backend_cfg)`; inject `event_bus` into any satisfying `EventBusAwareSpeakerBackend`. Resilient: one backend failing to start logs + continues; the service remains ready (`enabled=True` in `speaker.info`) as long as at least one backend started. A startup-failure list is exposed on `speaker.info` so the settings UI can surface "Sonos backend failed to start — check config" without blocking the rest.
- Re-init recreates only changed/added/dropped backends; untouched backends keep their connection state.

**Routing helpers (private):**

- `_route_id(sid: str) -> tuple[SpeakerBackend, str]` — splits, looks up, raises `KeyError` if backend not loaded.
- `_route_ids(sids: list[str]) -> dict[str, list[str]]` — groups namespaced IDs by backend for fan-out.

**Dispatch:**

- **Single-speaker methods** (`set_volume`, `get_volume`, `get_playback_state`, `get_now_playing`, `stop_speakers([single])`): route through `_route_id`.
- **Multi-speaker methods** (`play_on_speakers`, `prepare_speakers`, `stop_speakers([many])`, `play_queue_on_speakers`, `enqueue_on_speakers`, `set_repeat_on_speakers`): group via `_route_ids`, fan out per backend in parallel via `asyncio.gather`, merge results.
- **`list_speakers()`**: `asyncio.gather(return_exceptions=True)` across `self._backends.values()`, stamp `backend_name`, namespace `speaker_id`, log + skip per-backend exceptions so an unreachable cluster doesn't blank the list.
- **`group_speakers([s1, s2, …])`**: `_route_ids` returns `>1` entries → raise `ValueError("Cannot group speakers across backends — sonos:living and browser:alice live on different audio systems and can't be synchronized.")`. Else dispatch to the single backend.
- **`ungroup_speakers(sid)` / `list_speaker_groups()`**: route through `_route_id`; `list_speaker_groups` calls every backend reporting `supports_grouping=True` and stamps `backend_name` on each returned `SpeakerGroup`.
- **`cached_speakers`** refreshes inside `_reinit_backends` (not only at `start()`) so adding a backend via settings updates dropdowns without a service restart.

**Browser-echo gate** is detailed in §5.

---

## 3. Config schema

**Storage:** `gilbert.config` entity collection, `speaker` row.

```yaml
speaker:
  enabled: true
  primary_backend: sonos              # NEW — single-select; choices = enabled backends
  default_announce_volume: 60         # unchanged
  default_announce_speakers: []       # unchanged; entries are namespaced IDs after migration

  backends:                           # NEW — replaces single `backend` field
    sonos:
      enabled: true
      <sonos params>: ...
    local:
      enabled: false
      <local params>: ...
    browser:
      enabled: true
      <browser params>: ...
```

**`SpeakerService.config_params()`:**

- `primary_backend` — `ConfigParam(choices_from="speakers.enabled_backends")`. Dynamic choices resolved server-side to "names of currently-enabled SpeakerBackend subclasses." Satisfies the `feedback_no_freetext_for_references` memory.
- `backends.<name>.enabled` plus each backend's wrapped `backend_config_params()` — verbatim copy of `AIService.config_params()` lines ~1500–1521. AI-prompt fields are forwarded with `ai_prompt=bp.ai_prompt` per the rulebook category 5.
- The old `backend` single-select param is **removed**, not deprecated.

**Settings UI:** zero frontend code change. `ConfigSection.tsx:backendGroups()` already auto-renders `backends.<name>.*` keys as stacked per-backend cards. After this lands, the Speaker settings page visually mirrors AI Settings.

**`primary_backend` validation in `on_config_changed`:**

- If unset or pointing at a backend not in `self._backends`, auto-pick alphabetically-first enabled backend and log one-time WARN. Doesn't refuse to start.
- If `primary_backend` was set to a backend that just got disabled, same fallback.

**Legacy compatibility** (entity row, not `gilbert.yaml`): handled by the migration in §9.

---

## 4. Grouping policy + AI tool surface

**Grouping rules (enforced in `SpeakerService`):**

- `group_speakers(speaker_ids)` — split each via `_route_id`; if backend names disagree, raise `ValueError` with a user-readable message. AI tool layer catches and surfaces it as a clean tool error so the model can recover ("ok, I'll play it on both separately").
- `ungroup_speakers(sid)` and `list_speaker_groups()` are single-backend by construction.
- The `group_speakers` AI tool is **only exposed** when at least one loaded backend reports `supports_grouping=True`. Currently that's Sonos only. Disabling Sonos removes the tool from the AI surface.

**`speaker_id` parameter shape on AI tools:**

- All speaker-targeting tool params (`speaker_ids`, `target_speakers`, …) keep their existing form: `list[str]` of **names**, not namespaced IDs. `resolve_speaker_name()` does the name → namespaced ID translation server-side. The model never sees `sonos:RINCON_…`.
- `list_speakers` AI tool gains a `backend` field per entry. Description: "Each speaker is owned by a backend; cross-backend grouping isn't supported."

**Chat UI dropdowns:** today's dropdown sources `SpeakerInfo.name`. After this lands, the renderer adds a subtle secondary label (`"Living Room · sonos"`) only when there's a name collision. No collision → no secondary label.

---

## 5. Browser-echo refactor

**Current gate** stays for the "primary is browser" case:

```
event_bus + users_svc present
caller is real (not SYSTEM)
caller's `speaker.browser_echo` pref enabled
primary_backend != "browser"
```

**Second check added** to handle the explicit-target case. `play_on_speakers` resolves names → namespaced IDs first; the echo helper inspects that resolved list before deciding whether to fire:

```
explicit_targets = play_on_speakers's resolved (namespaced) target list, available
                   to _maybe_echo_to_browser as a parameter
if any t for t in explicit_targets where split_speaker_id(t) == ("browser", caller.user_id):
    skip echo
```

Behavior matrix:

| Scenario | Echo fires? |
|---|---|
| Bare `announce()` (primary = sonos) | ✓ to `browser:alice` |
| `play([sonos:living])` | ✓ to `browser:alice` |
| `play([browser:alice])` (caller is Alice) | ✗ (would double-play) |
| `play([sonos:living, browser:alice])` (caller is Alice) | ✗ (Alice's browser already a target) |
| `play([browser:bob])` (caller is Alice) | ✓ to `browser:alice` (Bob's browser is independent) |
| `primary_backend == "browser"` | ✗ (gate 1 fires) |

The gate reads `self._primary_backend` and the in-flight resolved target list. No new ContextVars; identity still flows through `interfaces.context.get_current_user()`.

---

## 6. MusicService changes

**6.1 New `MusicBackend.compatible_speaker_backends() -> frozenset[str]`** on `interfaces/music.py`. Returns the set of speaker-backend **names** whose `play_uri` can consume URIs this music backend produces.

- `SonosMusic` returns `frozenset({"sonos"})` — its URIs are S2 streams / SMAPI refs.
- A future generic-HTTP music backend returns `frozenset({"sonos", "local", "browser"})`.
- `frozenset({"*"})` is the wildcard sentinel for "trust me, my URIs play anywhere."

**6.2 `MusicService` validates targets in:**

- `play_item()`, `add_to_queue()`, `play_queue_on_speakers()`, `enqueue_on_speakers()`, `start_station()`.

Flow:

1. Resolve `speaker_names: list[str]` → namespaced IDs via `speaker_svc.resolve_names(names)` (new helper returning `dict[name, namespaced_id]`).
2. Group resolved IDs by `backend_name`.
3. If any backend in the group isn't in `self._backend.compatible_speaker_backends()` (and the set isn't `{"*"}`), raise `MusicSearchUnavailableError("music backend 'sonos' can't play to speaker 'Brian's Browser' (browser backend) — try a Sonos speaker.")`. AI tool catches and surfaces.

**6.3 `supports_loop` cross-check** (today: `speaker_svc.backend.supports_repeat`) becomes:

```python
return self._backend.supports_loop and any(
    sb.supports_repeat
    for name, sb in speaker_svc.backends.items()
    if (compat := self._backend.compatible_speaker_backends()) == {"*"} or name in compat
)
```

Reads `.backends` (mapping). The loop tool surfaces iff music supports loop AND at least one *compatible* loaded speaker backend supports repeat.

**6.4 Tool descriptions** — `play_music` etc. pick up a one-liner: "Music plays only on speakers compatible with the current music backend. Use `list_speakers` to see which qualify."

**Out of scope:** turning MusicService itself into a multi-backend aggregator. That's a separate refactor.

---

## 7. Other downstream consumers

**Reaches into `speaker_svc.backend` (singular) — must update because the property goes away:**

- `core/services/music.py:464` — covered in §6.
- `std-plugins/guess-that-song/service.py:91` — audit at implementation; rewrite as `speaker_svc.get_backend(name)` or lift to a higher-level capability.
- Any other hit from `grep -rn "speaker_svc\.backend\b" src/ std-plugins/`.

**Pass-through consumers (no logic change required):**

- `core/services/doorbell.py:290` — `play_on_speakers(speaker_names=…)`.
- `core/services/greeting.py:710`, `core/services/roast.py:238`, `core/services/audio_output.py:229`, `core/services/agent.py` announcements.
- `std-plugins/radio/` — DJ playback.

`resolve_speaker_name()` continues to accept names → namespaced IDs, so these don't need code changes. Verify their tests don't assert on the post-resolution ID shape.

**Scheduler:** stored tool args carry names. No code change.

**Configuration dynamic choices** (`core/services/configuration.py:507`, `choices_from="speakers"`): values become namespaced IDs; the `<Select>` label stays `name` and adds a `· backend` secondary line only when names collide.

**Singular `.backend` removal** breaks anything still typed against it. Run `mypy src/` after removal — failures map exactly to the callsites that need updating.

---

## 8. Frontend impact

**8.1 `speaker.info` WS RPC** shape change:

```ts
{
  enabled: boolean,
  primary_backend: string,
  active_backends: string[],
  startup_failures: { name: string, error: string }[],   // non-empty when a backend failed to initialize
}
```

Consumers update:

- `useBrowserSpeaker.tsx`: mount audio bubbles iff `active_backends.includes("browser")`.
- `useBrowserEchoPref.tsx`: disable the toggle when `primary_backend === "browser"` AND `active_backends.length === 1` (only browser active → echo would always double-play). Otherwise enabled.

**8.2 Speakers `<Select>` rendering** (wherever `choices_from="speakers"` is rendered):

- Option `value` is namespaced ID; option `label` is `name`.
- When two options share `label` in the same select, add a subtle secondary line (`<span class="text-muted">· backend</span>`) on each conflicting row.

**8.3 Settings page** — no code change. `ConfigSection.tsx:backendGroups()` already auto-renders `backends.<name>.*` as stacked per-backend cards. `primary_backend` ConfigParam renders as a single-select above them.

**8.4 `BrowserAudioBubbles.tsx`** — no change. Event shape and routing are unchanged; only the server-side decision to fire echo moved.

---

## 9. Migration

`src/gilbert/migrations/0001_namespace_speaker_aliases_and_config.py` — single boot-time migration. (Currently the next-available core migration number; no numbered migrations exist yet under `src/gilbert/migrations/`.) Idempotent (the migration runner re-runs scripts after crashes; idempotency is mandatory per CLAUDE.md).

**Alias rows in `speaker_aliases` collection:**

```python
for row in storage.query(Query(collection="speaker_aliases")):
    if ":" in row["speaker_id"]:
        continue                                  # already namespaced
    native_id = row["speaker_id"]
    matches = []
    for name, backend in speaker_svc._backends.items():
        if any(s.speaker_id == native_id for s in await backend.list_speakers()):
            matches.append(name)
    if len(matches) == 1:
        chosen = matches[0]
    elif len(matches) > 1:
        chosen = "sonos" if "sonos" in matches else sorted(matches)[0]
        logger.warning("alias %s matches multiple backends %s — chose %s", row["alias"], matches, chosen)
    else:
        logger.info("no loaded backend recognizes alias %s=%s — left bare", row["alias"], native_id)
        continue
    row["speaker_id"] = f"{chosen}:{native_id}"
    storage.put(row)
```

**`speaker` config entity row:**

```python
row = storage.get("gilbert.config", "speaker")
if row and "backend" in row and "primary_backend" not in row:
    backend_name = row["backend"]
    row.setdefault("backends", {}).setdefault(backend_name, {})["enabled"] = True
    row["primary_backend"] = backend_name
    del row["backend"]
    storage.put(row)
    logger.info("migrated legacy speaker.backend=%s → primary + backends.%s.enabled", backend_name, backend_name)
```

**Compat shim** in `resolve_speaker_name`: if a stored alias's `speaker_id` doesn't contain `":"` (post-migration leftovers), scan loaded backends for a native-ID match; prefer the first hit. Logged at DEBUG.

---

## 10. Tests

**New under `tests/unit/`:**

- `test_speaker_service_multi_backend.py` — two fake backends; merged `list_speakers` namespacing; mixed-target fan-out via `asyncio.gather`; one backend's `list_speakers` raising → others still returned; `group_speakers` cross-backend raises; `_reinit_backends` add/drop/change; `primary_backend` fallback when unset / disabled.
- `test_speaker_browser_echo_multi_backend.py` (extend `test_speaker_browser_echo.py`) — echo skipped when caller's `browser:<uid>` is in target set; echo fires for non-browser explicit targets; echo skipped when `primary_backend == "browser"`; mixed-backend target with caller's browser → no echo.
- `test_music_service_compatible_backends.py` — `compatible_speaker_backends()` validation rejects mismatched targets; `{"*"}` wildcard accepts everything; `supports_loop` reads `.backends` mapping correctly.

**New under `tests/integration/`:**

- `test_speaker_alias_migration.py` — seed bare alias rows + mixed namespaced rows; run migration; assert namespacing + idempotency on a second run; test multi-backend match ambiguity (prefer sonos + warn).
- `test_speaker_config_migration.py` — seed legacy `backend: sonos` config entity; run migration; assert `primary_backend = sonos`, `backends.sonos.enabled = True`, legacy `backend` key removed.

**Existing tests that need updating:**

- `tests/unit/test_speaker_service.py` — fake backend uses unprefixed IDs; either prefix the fixture or let the service namespace. Approximately 10 assertions adjust to expect `fake:uid-1` etc.
- `tests/unit/test_browser_speaker.py` — already namespaces; tests should pass unchanged.
- Music / doorbell / radio / greeting / roast / scheduler tests that mock `play_on_speakers` — names unchanged; only update assertions that read post-resolution ID shape.

---

## 11. Phasing

**One PR (bundled into PR #16), ~7 commits in this order:**

1. **Interface + namespacing groundwork** — add `backend_name` fields, `split_speaker_id`, namespace IDs flowing through the still-single-backend service. Update existing tests. Compiles green even though only one backend is loaded.
2. **Service multi-backend core** — `self._backends: dict`, `_reinit_backends`, routing helpers, dispatch updates, grouping rejection, `cached_speakers` refresh.
3. **Config schema + migration** — `primary_backend` + `backends.<name>.*` ConfigParams, migration script (aliases + config row), settings UI verified.
4. **Browser-echo refined gate** — second check (skip if caller's browser in target).
5. **MusicService** — `compatible_speaker_backends()`, validation, `supports_loop` updated.
6. **Other consumers** — guess-that-song fix, remaining `.backend` reaches, downstream test updates.
7. **Docs** — `docs/architecture/speaker-system.md`, `CLAUDE.md`, `README.md` (if it references single-backend speakers).

Reviewer can stop after any commit and the tree is still green. After the last commit lands on `feature/browser_speaker_backend`, the bundle ships via `/merge-pr 16` (squash to main).

---

## Open / deferred items

None. All open questions from the brainstorming session were resolved during walkthrough.

## Out of scope (explicitly deferred)

- Multiple instances of the same backend type (two Sonos households, two LAN setups).
- Aggregator pattern for `MusicService` itself.
- Stereo-pair / true cross-backend audio sync (would require a synchronization layer Gilbert doesn't have).
- Removal of bare-ID compat shim in `resolve_speaker_name` — kept as a long-term safety net.
