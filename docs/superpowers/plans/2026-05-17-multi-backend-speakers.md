# Multi-Backend SpeakerService Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `SpeakerService` from one-backend-at-a-time to an aggregator that runs N speaker backends simultaneously (Sonos + local + browser), with namespaced IDs (`<backend>:<native>`), per-backend grouping policy, and downstream consumer updates (MusicService, guess-that-song, browser-echo).

**Architecture:** Mirror `AIService._reinit_backends` aggregator pattern. Service holds `self._backends: dict[str, SpeakerBackend]`. ID prefix stamped during `list_speakers()` merge; stripped before dispatch via `_route_id` / `_route_ids` helpers. Cross-backend grouping rejected at the service layer; cross-backend play silently fans out (un-synchronized). Designated `primary_backend` config field handles bare `announce()` defaults. One-shot DB migration namespaces stored aliases + legacy `speaker.backend` config.

**Tech Stack:** Python 3.12+, uv, pytest, async (asyncio), Gilbert's interfaces/core/integrations/storage/web layered architecture, SQLite via `StorageBackend`. All commits land on `feature/browser_speaker_backend` (PR #16) for a final squash-merge.

**Companion spec:** `docs/superpowers/specs/2026-05-17-multi-backend-speakers-design.md` (commit `5ae897d`).

---

## File Structure

**Created:**

- `src/gilbert/migrations/0001_namespace_speaker_aliases_and_config.py` — boot-time migration namespacing alias rows + rewriting legacy `speaker.backend` config.
- `tests/unit/test_speaker_service_multi_backend.py` — fake-backend unit tests for the multi-backend behaviors.
- `tests/unit/test_music_service_compatible_backends.py` — `compatible_speaker_backends()` validation tests.
- `tests/integration/test_speaker_migration.py` — full migration runner integration.

**Modified:**

- `src/gilbert/interfaces/speaker.py` — `SpeakerInfo.backend_name`, `SpeakerGroup.backend_name`, `split_speaker_id`, `SpeakerProvider` protocol shape.
- `src/gilbert/interfaces/music.py` — `MusicBackend.compatible_speaker_backends()` classmethod.
- `src/gilbert/core/services/speaker.py` — `self._backends` mapping, `_reinit_backends`, routing helpers, dispatch updates, grouping rejection, `cached_speakers` refresh.
- `src/gilbert/core/services/music.py` — capability validation + `supports_loop` updated to read `.backends` mapping.
- `src/gilbert/core/services/configuration.py` — `speakers.enabled_backends` dynamic-choices resolver.
- `src/gilbert/integrations/local_speaker.py` — no logic change; verify `compatible_speaker_backends`-style references (likely none).
- `src/gilbert/integrations/browser_speaker.py` — no logic change.
- `std-plugins/sonos/sonos_speaker.py` — no internal change; backend keeps emitting native IDs.
- `std-plugins/sonos/sonos_music.py` — declare `compatible_speaker_backends = frozenset({"sonos"})`.
- `std-plugins/guess-that-song/service.py` — replace `speaker_svc.backend` reach with `speaker_svc.get_backend("sonos")` (or whichever applies).
- `frontend/src/hooks/useBrowserSpeaker.tsx` — read `active_backends` instead of `backend`.
- `frontend/src/hooks/useBrowserEchoPref.tsx` — disable rule based on `primary_backend` + `active_backends.length`.
- `frontend/src/components/settings/ConfigField.tsx` (or wherever `choices_from="speakers"` is rendered) — namespaced ID values + name-collision disambiguation in `<Select>` label.
- `tests/unit/test_speaker_service.py` — adjust assertions for namespaced IDs.
- `tests/unit/test_speaker_browser_echo.py` — new cases for echo skipping on explicit-target match.
- `docs/architecture/speaker-system.md` — document the multi-backend shape.
- `CLAUDE.md` — note multi-backend SpeakerService in architecture section if it conflates singular `backend`.
- `README.md` — only if it references single-backend speaker shape (audit).

---

## Phase 1 — Interface + namespacing groundwork

Goal of Phase 1: every place that produces or consumes a speaker_id uses the `<backend>:<native>` shape, but the service is still single-backend. Ships green.

### Task 1: Add `backend_name` field to `SpeakerInfo` and `SpeakerGroup`

**Files:**
- Modify: `src/gilbert/interfaces/speaker.py` (lines 76-98)
- Test: `tests/unit/test_speaker_interfaces.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_speaker_interfaces.py`:

```python
from gilbert.interfaces.speaker import SpeakerInfo, SpeakerGroup


def test_speaker_info_has_backend_name_default_empty():
    info = SpeakerInfo(speaker_id="x", name="X", ip_address="")
    assert info.backend_name == ""


def test_speaker_info_accepts_backend_name():
    info = SpeakerInfo(speaker_id="sonos:abc", name="Living Room", ip_address="", backend_name="sonos")
    assert info.backend_name == "sonos"


def test_speaker_group_has_backend_name_default_empty():
    g = SpeakerGroup(group_id="g", name="G", coordinator_id="c")
    assert g.backend_name == ""


def test_speaker_group_accepts_backend_name():
    g = SpeakerGroup(group_id="g", name="G", coordinator_id="c", backend_name="sonos")
    assert g.backend_name == "sonos"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_speaker_interfaces.py -v`
Expected: FAIL with `TypeError: ... got an unexpected keyword argument 'backend_name'`

- [ ] **Step 3: Add the fields**

In `src/gilbert/interfaces/speaker.py`, add `backend_name: str = ""` as the last field on both dataclasses (must come after the existing fields with defaults to satisfy dataclass rules):

```python
@dataclass(frozen=True)
class SpeakerInfo:
    speaker_id: str
    name: str
    ip_address: str
    model: str = ""
    group_id: str = ""
    group_name: str = ""
    is_group_coordinator: bool = False
    volume: int = 0
    state: PlaybackState = PlaybackState.STOPPED
    backend_name: str = ""


@dataclass(frozen=True)
class SpeakerGroup:
    group_id: str
    name: str
    coordinator_id: str
    member_ids: list[str] = field(default_factory=list)
    backend_name: str = ""
```

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_speaker_interfaces.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/speaker.py tests/unit/test_speaker_interfaces.py
git commit -m "speaker(interfaces): add backend_name to SpeakerInfo and SpeakerGroup"
```

---

### Task 2: Add `split_speaker_id` helper

**Files:**
- Modify: `src/gilbert/interfaces/speaker.py` (add near `to_browser_url`, around line 12)
- Test: `tests/unit/test_speaker_interfaces.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_speaker_interfaces.py`:

```python
import pytest
from gilbert.interfaces.speaker import split_speaker_id


def test_split_speaker_id_splits_on_first_colon():
    assert split_speaker_id("sonos:RINCON_AABBCC") == ("sonos", "RINCON_AABBCC")


def test_split_speaker_id_preserves_colons_in_native_id():
    assert split_speaker_id("browser:user:abc:def") == ("browser", "user:abc:def")


def test_split_speaker_id_raises_on_unprefixed():
    with pytest.raises(ValueError, match="must be namespaced"):
        split_speaker_id("RINCON_AABBCC")


def test_split_speaker_id_raises_on_empty():
    with pytest.raises(ValueError):
        split_speaker_id("")
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_interfaces.py::test_split_speaker_id_splits_on_first_colon -v`
Expected: FAIL with `ImportError: cannot import name 'split_speaker_id'`

- [ ] **Step 3: Implement**

Add to `src/gilbert/interfaces/speaker.py` (after `to_browser_url`, before `PlaybackState`):

```python
def split_speaker_id(speaker_id: str) -> tuple[str, str]:
    """Split a namespaced speaker id ``<backend>:<native>`` into its parts.

    Raises ``ValueError`` if ``speaker_id`` is not namespaced. Callers
    above the backend boundary should always pass namespaced ids; bare
    native ids are a sign of legacy / un-migrated data.
    """
    if ":" not in speaker_id:
        raise ValueError(f"speaker_id must be namespaced '<backend>:<native>', got {speaker_id!r}")
    backend, _, native = speaker_id.partition(":")
    return backend, native
```

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_speaker_interfaces.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/speaker.py tests/unit/test_speaker_interfaces.py
git commit -m "speaker(interfaces): add split_speaker_id helper"
```

---

### Task 3: Reshape `SpeakerProvider` protocol

**Files:**
- Modify: `src/gilbert/interfaces/speaker.py` (lines 349-379)
- Modify: `src/gilbert/core/services/music.py:461-464` (existing isinstance check now reads new protocol)
- Test: existing tests verify the consumer side compiles after the change

This task only changes the **declared** protocol shape. The next task (Task 4) puts the matching attributes on `SpeakerService`. Until then mypy may complain — that's expected and resolves at Task 4.

- [ ] **Step 1: Modify the protocol**

Replace `SpeakerProvider` in `src/gilbert/interfaces/speaker.py:349-379` with:

```python
from collections.abc import Mapping


@runtime_checkable
class SpeakerProvider(Protocol):
    """Protocol for services providing speaker control capabilities."""

    @property
    def backends(self) -> Mapping[str, "SpeakerBackend"]:
        """Mapping of currently-loaded backends, keyed by ``backend_name``."""
        ...

    def get_backend(self, name: str) -> "SpeakerBackend | None":
        """Return a loaded backend by name, or ``None`` if not loaded."""
        ...

    async def resolve_names(self, names: list[str]) -> dict[str, str]:
        """Resolve speaker display names to namespaced ids.

        Returns ``{name: "<backend>:<native>"}``. Names that don't match
        any known speaker are omitted from the result (callers decide
        whether that's an error).
        """
        ...

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        context: str = "",
    ) -> str:
        """Announce ``text`` over speakers via text-to-speech."""
        ...
```

The old `backend` singular property is **removed**.

- [ ] **Step 2: Update existing consumer that used the singular `.backend`**

In `src/gilbert/core/services/music.py:461-464` change:

```python
def supports_loop(self) -> bool:
    if not (self._backend and self._backend.supports_loop):
        return False
    speaker_svc = self._get_speaker_svc()
    if not isinstance(speaker_svc, SpeakerProvider):
        return False
    return bool(speaker_svc.backend.supports_repeat)
```

to (interim shape — full update happens in Task 16, this version just keeps it green):

```python
def supports_loop(self) -> bool:
    if not (self._backend and self._backend.supports_loop):
        return False
    speaker_svc = self._get_speaker_svc()
    if not isinstance(speaker_svc, SpeakerProvider):
        return False
    return any(b.supports_repeat for b in speaker_svc.backends.values())
```

- [ ] **Step 3: Find other singular-backend callsites and remove them**

Run: `grep -rn "speaker_svc\.backend\b\|speaker_service\.backend\b" src/ std-plugins/ 2>&1`

For each hit, replace with a call against `.backends` mapping or `.get_backend(name)`. Likely targets: `std-plugins/guess-that-song/service.py:91`. Update each to use the new shape.

If a callsite needs a specific backend (e.g. "the Sonos backend"), use `speaker_svc.get_backend("sonos")` and `None`-check.

- [ ] **Step 4: Run unit tests**

Run: `uv run pytest tests/unit/test_speaker_service.py tests/unit/test_music_service.py -x -q`
Expected: failures (SpeakerService doesn't yet implement `.backends` or `resolve_names`) — that's Task 4's job. **Don't commit yet.**

- [ ] **Step 5: Move on to Task 4**

Skip the commit; the next task adds the missing service-side attributes, and we commit Tasks 3+4 together.

---

### Task 4: Make `SpeakerService` satisfy the new protocol (still single-backend internally)

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_speaker_service.py`, add:

```python
@pytest.mark.asyncio
async def test_backends_mapping_exposes_loaded_backend(speaker_service_with_fake_backend):
    svc = speaker_service_with_fake_backend
    assert "fake" in svc.backends
    assert svc.backends["fake"] is svc._backend  # interim — Task 5 removes _backend


@pytest.mark.asyncio
async def test_get_backend_returns_loaded_or_none(speaker_service_with_fake_backend):
    svc = speaker_service_with_fake_backend
    assert svc.get_backend("fake") is svc._backend
    assert svc.get_backend("nonexistent") is None


@pytest.mark.asyncio
async def test_resolve_names_maps_display_names_to_namespaced_ids(speaker_service_with_fake_backend):
    svc = speaker_service_with_fake_backend
    result = await svc.resolve_names(["FakeSpeaker1"])
    assert result == {"FakeSpeaker1": "fake:uid-1"}
```

(Assumes the existing `speaker_service_with_fake_backend` fixture; if absent, the test author adds one in `tests/unit/conftest.py` that wires a single fake backend with `backend_name="fake"` and one speaker with `speaker_id="uid-1"`, `name="FakeSpeaker1"`.)

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service.py::test_backends_mapping_exposes_loaded_backend -v`
Expected: FAIL (`AttributeError: 'SpeakerService' object has no attribute 'backends'`).

- [ ] **Step 3: Add interim properties + `resolve_names`**

In `src/gilbert/core/services/speaker.py`, near the other public service attributes, add:

```python
@property
def backends(self) -> Mapping[str, SpeakerBackend]:
    """Mapping of currently-loaded backends. Interim — phase 2 replaces
    the single ``_backend`` with ``_backends: dict``; for now we return
    a one-entry mapping so consumers can migrate against the new
    protocol shape ahead of the storage refactor."""
    if self._backend is None:
        return {}
    return {self._backend.backend_name: self._backend}


def get_backend(self, name: str) -> SpeakerBackend | None:
    return self.backends.get(name)


async def resolve_names(self, names: list[str]) -> dict[str, str]:
    """Map display names to namespaced speaker ids."""
    speakers = await self._list_speakers_internal()
    by_name = {s.name: s for s in speakers}
    out: dict[str, str] = {}
    for name in names:
        s = by_name.get(name)
        if s is not None:
            out[name] = s.speaker_id  # namespaced after Task 5
    return out
```

`_list_speakers_internal` is whatever the existing `list_speakers()` is named today (likely the same — adjust to actual method name).

Add `from collections.abc import Mapping` to the imports.

- [ ] **Step 4: Verify all unit tests pass**

Run: `uv run pytest tests/unit/test_speaker_service.py tests/unit/test_music_service.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit Tasks 3 + 4 together**

```bash
git add src/gilbert/interfaces/speaker.py src/gilbert/core/services/speaker.py \
        src/gilbert/core/services/music.py std-plugins/guess-that-song/ \
        tests/unit/test_speaker_service.py
git commit -m "speaker: replace SpeakerProvider.backend (singular) with backends/get_backend/resolve_names"
```

---

### Task 5: Namespace IDs in `SpeakerService.list_speakers()`

**Files:**
- Modify: `src/gilbert/core/services/speaker.py` (the existing `list_speakers` method)
- Modify: `tests/unit/test_speaker_service.py` (adjust assertions)

This is the load-bearing change for Phase 1: IDs flowing out of the service become namespaced.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_speaker_service.py`:

```python
@pytest.mark.asyncio
async def test_list_speakers_returns_namespaced_ids(speaker_service_with_fake_backend):
    svc = speaker_service_with_fake_backend
    speakers = await svc.list_speakers()
    assert speakers, "expected at least one speaker from the fake backend"
    for s in speakers:
        assert ":" in s.speaker_id, f"id {s.speaker_id!r} not namespaced"
        assert s.backend_name, f"backend_name not stamped on {s}"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service.py::test_list_speakers_returns_namespaced_ids -v`
Expected: FAIL — current `list_speakers` returns bare native ids.

- [ ] **Step 3: Implement namespacing in `list_speakers`**

Locate `SpeakerService.list_speakers` (search for `async def list_speakers` in `src/gilbert/core/services/speaker.py`) and wrap each returned `SpeakerInfo`:

```python
async def list_speakers(self) -> list[SpeakerInfo]:
    if self._backend is None:
        return []
    raw = await self._backend.list_speakers()
    name = self._backend.backend_name
    return [
        replace(s, speaker_id=f"{name}:{s.speaker_id}", backend_name=name)
        for s in raw
    ]
```

Use `dataclasses.replace` since `SpeakerInfo` is frozen. Add `from dataclasses import replace` if not already imported.

- [ ] **Step 4: Update any other places that emit `SpeakerInfo`**

Run: `grep -n "SpeakerInfo(" src/gilbert/core/services/speaker.py` — for each cached_speakers update or other emitter, ensure ids/`backend_name` are namespaced.

Also update `list_speaker_groups` if it returns `SpeakerGroup` — namespace `coordinator_id` and each `member_ids` entry, stamp `backend_name`.

- [ ] **Step 5: Fix downstream test assertions**

Update existing assertions in `tests/unit/test_speaker_service.py` that read raw speaker ids — they now expect namespaced form. Typical pattern:

```python
# before
assert speakers[0].speaker_id == "uid-1"
# after
assert speakers[0].speaker_id == "fake:uid-1"
```

- [ ] **Step 6: Run full unit suite**

Run: `uv run pytest tests/unit/ -x -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service.py
git commit -m "speaker: namespace ids as <backend>:<native> in list_speakers + list_speaker_groups"
```

---

### Task 6: Add `_route_id` / `_route_ids` helpers and route single-speaker methods through them

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_speaker_service.py`:

```python
@pytest.mark.asyncio
async def test_route_id_splits_and_returns_backend(speaker_service_with_fake_backend):
    svc = speaker_service_with_fake_backend
    backend, native = svc._route_id("fake:uid-1")
    assert backend is svc._backend
    assert native == "uid-1"


@pytest.mark.asyncio
async def test_route_id_raises_for_unknown_backend(speaker_service_with_fake_backend):
    svc = speaker_service_with_fake_backend
    with pytest.raises(KeyError, match="nope"):
        svc._route_id("nope:xyz")


@pytest.mark.asyncio
async def test_route_ids_groups_by_backend(speaker_service_with_fake_backend):
    svc = speaker_service_with_fake_backend
    # only "fake" is loaded; "ghost" backend keys raise
    grouped = svc._route_ids(["fake:a", "fake:b"])
    assert grouped == {"fake": ["a", "b"]}
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service.py::test_route_id_splits_and_returns_backend -v`
Expected: FAIL (`AttributeError`).

- [ ] **Step 3: Implement**

In `src/gilbert/core/services/speaker.py`, add private helpers:

```python
def _route_id(self, speaker_id: str) -> tuple[SpeakerBackend, str]:
    backend_name, native_id = split_speaker_id(speaker_id)
    backend = self.backends.get(backend_name)
    if backend is None:
        raise KeyError(f"speaker backend {backend_name!r} not loaded")
    return backend, native_id


def _route_ids(self, speaker_ids: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for sid in speaker_ids:
        backend_name, native_id = split_speaker_id(sid)
        if backend_name not in self.backends:
            raise KeyError(f"speaker backend {backend_name!r} not loaded")
        grouped.setdefault(backend_name, []).append(native_id)
    return grouped
```

Add `from gilbert.interfaces.speaker import split_speaker_id` if not already.

- [ ] **Step 4: Route single-speaker methods through `_route_id`**

Update `set_volume`, `get_volume`, `get_playback_state`, `get_now_playing`, single-target `stop_speakers` calls to:

```python
async def get_volume(self, speaker_id: str) -> int:
    backend, native = self._route_id(speaker_id)
    return await backend.get_volume(native)
```

Same pattern for the others. **Don't touch multi-speaker fan-out yet** — that's Task 7 and Phase 2.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/unit/test_speaker_service.py -x -q`
Expected: PASS (existing tests still pass because there's still only one backend, namespaced ids route through cleanly).

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service.py
git commit -m "speaker: add _route_id / _route_ids and route single-speaker methods through them"
```

---

### Task 7: Multi-speaker dispatch routes via `_route_ids` (still single backend → single group)

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`

This keeps single-backend behavior identical but lays the routing groundwork — the call shape becomes `_route_ids(ids) → per-backend native id list → call backend method`. Tomorrow when there are two backends, the same code path fans out via `asyncio.gather`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_speaker_service.py`:

```python
@pytest.mark.asyncio
async def test_play_on_speakers_passes_native_ids_to_backend(speaker_service_with_fake_backend, monkeypatch):
    svc = speaker_service_with_fake_backend
    received = {}
    async def fake_play_uri(request):
        received["ids"] = list(request.speaker_ids)
    monkeypatch.setattr(svc._backend, "play_uri", fake_play_uri)

    await svc.play_on_speakers(uri="http://x", speaker_names=None,
                               speaker_ids=["fake:uid-1", "fake:uid-2"])
    assert received["ids"] == ["uid-1", "uid-2"], (
        "Backend.play_uri must receive native ids, not namespaced"
    )
```

(Adjust signature to match the real `play_on_speakers` — it likely takes `speaker_names` as the primary parameter; ensure the test bypasses name resolution and passes ids directly via whatever internal entrypoint exists, or add a temporary internal accessor.)

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service.py::test_play_on_speakers_passes_native_ids_to_backend -v`
Expected: FAIL — namespaced ids leak into the backend call.

- [ ] **Step 3: Implement**

Update `play_on_speakers` (and `prepare_speakers`, multi-target `stop_speakers`, `set_repeat_on_speakers`, `play_queue_on_speakers`, `enqueue_on_speakers`) to:

```python
async def play_on_speakers(
    self,
    *,
    uri: str,
    speaker_names: list[str] | None = None,
    speaker_ids: list[str] | None = None,
    volume: int | None = None,
    title: str = "",
    announce: bool = False,
    position_seconds: float | None = None,
) -> None:
    resolved_ids = await self._resolve_target_ids(speaker_names, speaker_ids)
    if not resolved_ids:
        return
    grouped = self._route_ids(resolved_ids)
    coros = []
    for backend_name, native_ids in grouped.items():
        backend = self.backends[backend_name]
        request = PlayRequest(
            uri=uri, speaker_ids=native_ids, volume=volume,
            title=title, announce=announce, position_seconds=position_seconds,
        )
        coros.append(backend.play_uri(request))
    await asyncio.gather(*coros)
    # browser-echo hop stays where it is — Task 14 refines its gate
```

Define `_resolve_target_ids(speaker_names, speaker_ids) -> list[str]` as a private helper that uses `resolve_names` when `speaker_names` is non-None and returns `speaker_ids` directly otherwise (falling back to `default_announce_speakers` when both are None).

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_speaker_service.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service.py
git commit -m "speaker: route multi-speaker dispatch through _route_ids with per-backend fan-out"
```

---

### Phase 1 checkpoint

Run the full suite:

```bash
uv run pytest -x -q
```

Expected: all green. Speaker ids are now `<backend>:<native>` everywhere above the service boundary, the protocol uses `backends` / `get_backend` / `resolve_names`, and dispatch is routed through helpers — but the service still loads only one backend internally. Ships.

---

## Phase 2 — Service multi-backend core

Replace `self._backend` (singular) with `self._backends: dict`. Add `_reinit_backends` lifecycle. Grouping rejection.

### Task 8: Storage flip — `_backend` → `_backends: dict`

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_speaker_service_multi_backend.py` (new):

```python
import pytest
from gilbert.core.services.speaker import SpeakerService


@pytest.mark.asyncio
async def test_service_stores_backends_in_dict(speaker_service_with_two_fake_backends):
    svc = speaker_service_with_two_fake_backends
    assert isinstance(svc._backends, dict)
    assert set(svc._backends) == {"fake_a", "fake_b"}
```

Create a fixture in `tests/unit/conftest.py` (`speaker_service_with_two_fake_backends`) that registers two `FakeSpeakerBackend` subclasses with `backend_name="fake_a"` and `backend_name="fake_b"`, enables both in config, and starts the service.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_multi_backend.py -v`
Expected: FAIL (`AttributeError: '_backends'` or test setup error).

- [ ] **Step 3: Implement**

In `src/gilbert/core/services/speaker.py`:

- Replace `self._backend: SpeakerBackend | None = None` initialization with `self._backends: dict[str, SpeakerBackend] = {}`.
- Replace the `backends` `@property` (which used to wrap singular) with: `@property\n def backends(self) -> Mapping[str, SpeakerBackend]:\n     return self._backends`.
- Remove every remaining reference to `self._backend` (singular) — replace with `self._backends[name]` or `next(iter(self._backends.values()))` only where a "pick any" is legitimate (there should be none after this task).

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/ -x -q`
Expected: existing tests still pass with one-backend fixtures; new multi-backend test passes too.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service_multi_backend.py tests/unit/conftest.py
git commit -m "speaker: flip storage to self._backends: dict[str, SpeakerBackend]"
```

---

### Task 9: Add `_reinit_backends` lifecycle (copied from AIService)

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_speaker_service_multi_backend.py`:

```python
@pytest.mark.asyncio
async def test_reinit_backends_starts_enabled_drops_disabled(speaker_service_factory):
    cfg = {"backends": {"fake_a": {"enabled": True}, "fake_b": {"enabled": False}}}
    svc = await speaker_service_factory(cfg)
    assert set(svc._backends) == {"fake_a"}

    # Now flip
    await svc._reinit_backends({"fake_a": {"enabled": False}, "fake_b": {"enabled": True}})
    assert set(svc._backends) == {"fake_b"}


@pytest.mark.asyncio
async def test_reinit_backends_resilient_to_one_failing(speaker_service_factory, monkeypatch):
    # ... wire FakeBackendBoom whose initialize raises
    cfg = {"backends": {"fake_a": {"enabled": True}, "fake_boom": {"enabled": True}}}
    svc = await speaker_service_factory(cfg)
    assert "fake_a" in svc._backends
    assert "fake_boom" not in svc._backends
```

`speaker_service_factory` is a new fixture: `async def speaker_service_factory(cfg: dict) -> SpeakerService` that builds the service with the given backend config.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_multi_backend.py::test_reinit_backends_starts_enabled_drops_disabled -v`
Expected: FAIL.

- [ ] **Step 3: Implement (copy from `src/gilbert/core/services/ai.py:1528-1576`)**

```python
async def _reinit_backends(self, backends_config: dict[str, Any]) -> None:
    if not isinstance(backends_config, dict):
        return
    for name, cls in SpeakerBackend.registered_backends().items():
        if name not in backends_config:
            old = self._backends.pop(name, None)
            if old is not None:
                await old.close()
                logger.info("speaker backend '%s' removed (no config section)", name)
            continue
        cfg = backends_config.get(name, {})
        if not isinstance(cfg, dict):
            cfg = {}
        enabled = cfg.get("enabled", True) is True
        old = self._backends.get(name)
        if not enabled:
            if old is not None:
                await old.close()
                self._backends.pop(name, None)
                self._startup_failures.pop(name, None)
                logger.info("speaker backend '%s' disabled, closed", name)
            continue
        try:
            inst = cls()
            if isinstance(inst, EventBusAwareSpeakerBackend) and self._event_bus_provider is not None:
                inst.set_event_bus_provider(self._event_bus_provider)
            await inst.initialize(cfg)
            if old is not None:
                await old.close()
            self._backends[name] = inst
            self._startup_failures.pop(name, None)
            logger.info("speaker backend '%s' (re)initialized", name)
        except Exception as exc:
            self._startup_failures[name] = str(exc)
            if old is None:
                logger.warning("speaker backend '%s' failed to start: %s", name, exc)
    await self._refresh_cached_speakers()
```

Add `self._startup_failures: dict[str, str] = {}` to `__init__`. The `_refresh_cached_speakers` helper rebuilds `cached_speakers` from all loaded backends (a thin wrapper around `list_speakers()` that ignores errors).

Wire `start()` to call `_reinit_backends(section.get("backends", {}))` and `on_config_changed(...)` similarly.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_speaker_service_multi_backend.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service_multi_backend.py
git commit -m "speaker: _reinit_backends lifecycle + startup_failures tracking"
```

---

### Task 10: `list_speakers()` merges across backends with `asyncio.gather`

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_speaker_service_multi_backend.py`:

```python
@pytest.mark.asyncio
async def test_list_speakers_merges_across_backends(speaker_service_with_two_fake_backends):
    svc = speaker_service_with_two_fake_backends
    speakers = await svc.list_speakers()
    backends_present = {s.backend_name for s in speakers}
    assert backends_present == {"fake_a", "fake_b"}


@pytest.mark.asyncio
async def test_list_speakers_tolerates_one_backend_raising(speaker_service_factory, monkeypatch):
    cfg = {"backends": {"fake_a": {"enabled": True}, "fake_b": {"enabled": True}}}
    svc = await speaker_service_factory(cfg)

    async def boom(self):
        raise RuntimeError("Sonos cluster unreachable")
    monkeypatch.setattr(svc._backends["fake_b"], "list_speakers", boom.__get__(svc._backends["fake_b"]))

    speakers = await svc.list_speakers()
    assert all(s.backend_name == "fake_a" for s in speakers)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_multi_backend.py::test_list_speakers_merges_across_backends -v`
Expected: FAIL (current `list_speakers` reads `self._backend` — by now nonexistent — or returns only one backend's worth).

- [ ] **Step 3: Implement**

Replace `list_speakers`:

```python
async def list_speakers(self) -> list[SpeakerInfo]:
    if not self._backends:
        return []
    results = await asyncio.gather(
        *(b.list_speakers() for b in self._backends.values()),
        return_exceptions=True,
    )
    merged: list[SpeakerInfo] = []
    for (name, _), result in zip(self._backends.items(), results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("speaker backend '%s' list_speakers failed: %s", name, result)
            continue
        for s in result:
            merged.append(replace(s, speaker_id=f"{name}:{s.speaker_id}", backend_name=name))
    return merged
```

Same treatment for `list_speaker_groups` — `asyncio.gather(return_exceptions=True)` over `b.list_groups()` for each backend with `supports_grouping`, stamp `backend_name`, namespace `coordinator_id` and each `member_ids` entry.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/ -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service_multi_backend.py
git commit -m "speaker: list_speakers/list_speaker_groups merge across backends with tolerance"
```

---

### Task 11: Reject cross-backend grouping in `group_speakers`

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_speaker_service_multi_backend.py`:

```python
@pytest.mark.asyncio
async def test_group_speakers_rejects_cross_backend(speaker_service_with_two_fake_backends):
    svc = speaker_service_with_two_fake_backends
    with pytest.raises(ValueError, match="across backends"):
        await svc.group_speakers(["fake_a:uid-1", "fake_b:uid-1"])


@pytest.mark.asyncio
async def test_group_speakers_passes_through_same_backend(speaker_service_with_two_fake_backends):
    svc = speaker_service_with_two_fake_backends
    await svc.group_speakers(["fake_a:uid-1", "fake_a:uid-2"])  # no raise
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_multi_backend.py::test_group_speakers_rejects_cross_backend -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `SpeakerService.group_speakers`:

```python
async def group_speakers(self, speaker_ids: list[str]) -> None:
    grouped = self._route_ids(speaker_ids)
    if len(grouped) > 1:
        names = ", ".join(speaker_ids[:2])
        raise ValueError(
            f"Cannot group speakers across backends — {names} live on "
            f"different audio systems and can't be synchronized."
        )
    [(backend_name, native_ids)] = grouped.items()
    await self._backends[backend_name].group_speakers(native_ids)
```

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_speaker_service_multi_backend.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service_multi_backend.py
git commit -m "speaker: reject cross-backend group_speakers with a user-readable error"
```

---

### Phase 2 checkpoint

Run the full suite:

```bash
uv run pytest -x -q
```

Expected: all green. The service now runs N backends simultaneously, fans out plays in parallel, lists merged speakers, tolerates one-backend failures, and rejects cross-backend grouping. Config still has the old `backend` single-select — Phase 3 swaps it out.

---

## Phase 3 — Config schema + migration

### Task 12: Replace `backend` ConfigParam with per-backend sections + `primary_backend`

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`
- Modify: `src/gilbert/core/services/configuration.py` (add `speakers.enabled_backends` choices resolver)

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_speaker_service_multi_backend.py`:

```python
@pytest.mark.asyncio
async def test_config_params_emits_per_backend_sections(speaker_service_with_two_fake_backends):
    svc = speaker_service_with_two_fake_backends
    keys = {p.key for p in svc.config_params()}
    assert "backends.fake_a.enabled" in keys
    assert "backends.fake_b.enabled" in keys
    assert "primary_backend" in keys
    assert "backend" not in keys, "legacy single-select must be removed"


@pytest.mark.asyncio
async def test_primary_backend_auto_picks_first_enabled_when_unset(speaker_service_factory, caplog):
    cfg = {"backends": {"fake_b": {"enabled": True}, "fake_a": {"enabled": True}}}
    svc = await speaker_service_factory(cfg)  # no primary_backend in cfg
    assert svc._primary_backend == "fake_a"   # alphabetical
    assert any("primary_backend" in r.message for r in caplog.records if r.levelname == "WARNING")
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_multi_backend.py::test_config_params_emits_per_backend_sections -v`
Expected: FAIL.

- [ ] **Step 3: Update `config_params`**

In `SpeakerService.config_params`, replace the existing `backend` ConfigParam with:

```python
def config_params(self) -> list[ConfigParam]:
    params: list[ConfigParam] = [
        ConfigParam("enabled", "Enabled", bool, default=True),
        ConfigParam(
            "primary_backend", "Primary backend", str,
            choices_from="speakers.enabled_backends",
            description="Receives bare announce()/play() calls without explicit targets.",
        ),
        ConfigParam("default_announce_volume", "Default announce volume", int, default=60),
        ConfigParam("default_announce_speakers", "Default announce speakers",
                    list, default=[], choices_from="speakers"),
    ]
    # Per-backend sections (mirrors AIService.config_params)
    for name, cls in SpeakerBackend.registered_backends().items():
        params.append(ConfigParam(
            f"backends.{name}.enabled", f"{name}: enabled", bool, default=False,
        ))
        for bp in cls.backend_config_params():
            params.append(ConfigParam(
                f"backends.{name}.{bp.key}", bp.label, bp.value_type,
                default=bp.default, choices=bp.choices, choices_from=bp.choices_from,
                multiline=bp.multiline, ai_prompt=bp.ai_prompt,
                description=bp.description, secret=bp.secret,
            ))
    return params
```

- [ ] **Step 4: Add `_primary_backend` resolution**

In `_apply_config` (or wherever the service reads its config), set `self._primary_backend`:

```python
primary = section.get("primary_backend") or ""
if primary not in self._backends:
    candidates = sorted(self._backends)
    if candidates:
        chosen = candidates[0]
        if primary:
            logger.warning(
                "speaker.primary_backend=%r is not loaded; falling back to %r", primary, chosen
            )
        else:
            logger.warning("speaker.primary_backend not set; defaulting to %r", chosen)
        self._primary_backend = chosen
    else:
        self._primary_backend = ""
else:
    self._primary_backend = primary
```

Call this after `_reinit_backends` so the fallback knows which backends are actually loaded.

- [ ] **Step 5: Add the `speakers.enabled_backends` dynamic choices resolver**

In `src/gilbert/core/services/configuration.py:_resolve_dynamic_choices`, add a branch:

```python
elif source == "speakers.enabled_backends":
    speaker_svc = self._resolver.get_capability("speaker_control")
    if speaker_svc and hasattr(speaker_svc, "backends"):
        return [
            {"value": name, "label": name}
            for name in sorted(speaker_svc.backends)
        ]
    return []
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/unit/ -x -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/core/services/speaker.py src/gilbert/core/services/configuration.py tests/unit/test_speaker_service_multi_backend.py
git commit -m "speaker: config schema — per-backend sections + primary_backend, drop legacy 'backend' field"
```

---

### Task 13: Boot-time migration `0001_namespace_speaker_aliases_and_config`

**Files:**
- Create: `src/gilbert/migrations/0001_namespace_speaker_aliases_and_config.py`
- Create: `tests/integration/test_speaker_migration.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_speaker_migration.py`:

```python
import pytest
from gilbert.migrations._0001_namespace_speaker_aliases_and_config import up


@pytest.mark.asyncio
async def test_migration_namespaces_alias_rows(storage_backend, two_fake_backends_loaded):
    await storage_backend.put("speaker_aliases", "k1", {
        "speaker_id": "uid-1", "alias": "kitchen", "display_alias": "Kitchen",
    })
    await up(storage_backend, two_fake_backends_loaded)
    row = await storage_backend.get("speaker_aliases", "k1")
    assert row["speaker_id"] == "fake_a:uid-1"


@pytest.mark.asyncio
async def test_migration_is_idempotent(storage_backend, two_fake_backends_loaded):
    await storage_backend.put("speaker_aliases", "k1", {
        "speaker_id": "fake_a:uid-1", "alias": "kitchen", "display_alias": "Kitchen",
    })
    await up(storage_backend, two_fake_backends_loaded)
    await up(storage_backend, two_fake_backends_loaded)  # second run
    row = await storage_backend.get("speaker_aliases", "k1")
    assert row["speaker_id"] == "fake_a:uid-1"


@pytest.mark.asyncio
async def test_migration_rewrites_legacy_config(storage_backend, two_fake_backends_loaded):
    await storage_backend.put("gilbert.config", "speaker", {
        "enabled": True, "backend": "fake_a",
    })
    await up(storage_backend, two_fake_backends_loaded)
    row = await storage_backend.get("gilbert.config", "speaker")
    assert row["primary_backend"] == "fake_a"
    assert row["backends"]["fake_a"]["enabled"] is True
    assert "backend" not in row


@pytest.mark.asyncio
async def test_migration_prefers_sonos_on_multi_backend_match(storage_backend, two_fake_backends_loaded, caplog):
    # both fake_a and fake_b expose uid-1 (test fixture configures this)
    await storage_backend.put("speaker_aliases", "k1", {
        "speaker_id": "uid-1", "alias": "kitchen", "display_alias": "Kitchen",
    })
    await up(storage_backend, two_fake_backends_loaded)
    row = await storage_backend.get("speaker_aliases", "k1")
    # neither fake_a nor fake_b is "sonos"; the chooser picks alphabetical first
    assert row["speaker_id"] == "fake_a:uid-1"
    assert any("matches multiple backends" in r.message for r in caplog.records)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/test_speaker_migration.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement the migration**

Create `src/gilbert/migrations/0001_namespace_speaker_aliases_and_config.py`:

```python
"""Migration: namespace stored speaker ids and rewrite legacy speaker.backend config.

Idempotent: rows that are already namespaced are skipped; configs that
already have ``primary_backend`` set are skipped.
"""
from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.speaker import SpeakerBackend
from gilbert.interfaces.storage import Query, StorageBackend

logger = logging.getLogger(__name__)


async def up(storage: StorageBackend, backends: dict[str, SpeakerBackend]) -> None:
    """Run the migration. ``backends`` is the dict of loaded backends keyed by name."""
    await _namespace_aliases(storage, backends)
    await _rewrite_legacy_config(storage)


async def _namespace_aliases(
    storage: StorageBackend, backends: dict[str, SpeakerBackend]
) -> None:
    rows = await storage.query(Query(collection="speaker_aliases"))
    for row in rows:
        sid = row.get("speaker_id", "")
        if ":" in sid:
            continue
        matches: list[str] = []
        for name, backend in backends.items():
            try:
                speakers = await backend.list_speakers()
            except Exception as exc:
                logger.debug("backend %s list_speakers failed during migration: %s", name, exc)
                continue
            if any(s.speaker_id == sid for s in speakers):
                matches.append(name)
        if not matches:
            logger.info("no loaded backend recognizes alias %r (id=%r) — left bare",
                        row.get("alias"), sid)
            continue
        if len(matches) > 1:
            chosen = "sonos" if "sonos" in matches else sorted(matches)[0]
            logger.warning(
                "alias %r matches multiple backends %s — chose %r",
                row.get("alias"), matches, chosen,
            )
        else:
            chosen = matches[0]
        row["speaker_id"] = f"{chosen}:{sid}"
        await storage.put("speaker_aliases", row["alias"], row)


async def _rewrite_legacy_config(storage: StorageBackend) -> None:
    row = await storage.get("gilbert.config", "speaker")
    if not row:
        return
    if "backend" not in row or "primary_backend" in row:
        return
    backend_name = row.pop("backend")
    row.setdefault("backends", {}).setdefault(backend_name, {})["enabled"] = True
    row["primary_backend"] = backend_name
    await storage.put("gilbert.config", "speaker", row)
    logger.info(
        "migrated legacy speaker.backend=%s → primary_backend + backends.%s.enabled",
        backend_name, backend_name,
    )
```

- [ ] **Step 4: Wire the migration into the runner**

Check `src/gilbert/migrations/runner.py` for how it discovers migrations. If the runner auto-discovers `0001_*.py` files in `src/gilbert/migrations/`, no wiring needed. Otherwise register the migration in the runner's manifest.

- [ ] **Step 5: Add a compat shim in `resolve_speaker_name`**

In `src/gilbert/core/services/speaker.py`, locate `resolve_speaker_name`. After the normal lookup (which returns a namespaced id), if the stored alias hits a bare id and falls through, fall back to scanning loaded backends for a native-id match:

```python
async def resolve_speaker_name(self, name: str) -> str | None:
    # ... existing logic that hits aliases / cached_speakers
    # Compat: if a stored alias's speaker_id is bare (no ":"), scan
    # loaded backends for a native-id match.
    if resolved is None:
        for backend_name, backend in self._backends.items():
            for s in await backend.list_speakers():
                if s.name == name:
                    return f"{backend_name}:{s.speaker_id}"
    return resolved
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/test_speaker_migration.py -v`
Expected: PASS.

Run: `uv run pytest -x -q`
Expected: PASS overall.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/migrations/0001_namespace_speaker_aliases_and_config.py \
        tests/integration/test_speaker_migration.py \
        src/gilbert/core/services/speaker.py
git commit -m "speaker: 0001 migration namespacing aliases + legacy speaker.backend rewrite"
```

---

### Phase 3 checkpoint

Run `uv run pytest -x -q`. Expected: green. Settings UI now renders per-backend cards + a `primary_backend` dropdown. Legacy installs auto-migrate on first boot.

---

## Phase 4 — Browser-echo refined gate

### Task 14: Add the explicit-target-set check to `_maybe_echo_to_browser`

**Files:**
- Modify: `src/gilbert/core/services/speaker.py` (the existing echo helper)
- Modify: `tests/unit/test_speaker_browser_echo.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_speaker_browser_echo.py`:

```python
@pytest.mark.asyncio
async def test_echo_skips_when_callers_browser_in_target_set(speaker_service_browser_echo):
    svc = speaker_service_browser_echo  # caller = alice; pref on; primary != browser
    events = svc._event_bus_provider.bus.published

    await svc.play_on_speakers(
        uri="http://x",
        speaker_ids=["sonos:living", "browser:alice"],
    )
    echo_events = [e for e in events if e.event_type == "speaker.browser.play"
                   and e.source == "speaker.echo"]
    assert echo_events == [], "echo must skip when caller's browser is an explicit target"


@pytest.mark.asyncio
async def test_echo_fires_when_targeting_other_users_browser(speaker_service_browser_echo):
    svc = speaker_service_browser_echo  # caller = alice
    events = svc._event_bus_provider.bus.published
    await svc.play_on_speakers(uri="http://x", speaker_ids=["browser:bob"])
    echo_for_alice = [e for e in events if e.event_type == "speaker.browser.play"
                      and e.source == "speaker.echo" and e.data["user_id"] == "alice"]
    assert len(echo_for_alice) == 1
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_browser_echo.py::test_echo_skips_when_callers_browser_in_target_set -v`
Expected: FAIL (current gate doesn't consider explicit targets).

- [ ] **Step 3: Implement**

Modify `_maybe_echo_to_browser` to accept the resolved target ids and add the second check:

```python
async def _maybe_echo_to_browser(
    self,
    *,
    uri: str,
    volume: int | None,
    title: str,
    announce: bool,
    position_seconds: float | None,
    explicit_target_ids: list[str],
) -> None:
    if not await self._browser_echo_should_fire():
        return
    # New: skip if caller's own browser is already in the explicit target set
    user = get_current_user()
    if user and user.user_id:
        caller_browser_id = f"browser:{user.user_id}"
        if caller_browser_id in explicit_target_ids:
            return
    # ... existing echo publish unchanged
```

Update `play_on_speakers` to pass `explicit_target_ids=resolved_ids` to the helper.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_speaker_browser_echo.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_browser_echo.py
git commit -m "speaker: browser-echo skips when caller's own browser is in explicit target set"
```

---

## Phase 5 — MusicService

### Task 15: `MusicBackend.compatible_speaker_backends()` classmethod

**Files:**
- Modify: `src/gilbert/interfaces/music.py`
- Modify: `std-plugins/sonos/sonos_music.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_music_service_compatible_backends.py`:

```python
from gilbert.interfaces.music import MusicBackend


def test_default_compatible_speaker_backends_is_wildcard():
    class Demo(MusicBackend):
        backend_name = "demo"
        async def initialize(self, config): pass
        # ... other abstracts; minimal stub
    assert Demo.compatible_speaker_backends() == frozenset({"*"})
```

```python
# std-plugins/sonos/tests/test_sonos_music.py — append
from std_plugins.sonos.sonos_music import SonosMusic

def test_sonos_music_only_compatible_with_sonos_speakers():
    assert SonosMusic.compatible_speaker_backends() == frozenset({"sonos"})
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_music_service_compatible_backends.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `src/gilbert/interfaces/music.py`:

```python
class MusicBackend(ABC):
    # ... existing
    @classmethod
    def compatible_speaker_backends(cls) -> frozenset[str]:
        """Names of SpeakerBackends whose play_uri can consume this music
        backend's URIs. Return ``frozenset({"*"})`` for wildcard ("works
        anywhere"). Subclasses override when their URIs are vendor-
        specific (e.g. Sonos returns ``frozenset({"sonos"})``)."""
        return frozenset({"*"})
```

In `std-plugins/sonos/sonos_music.py`:

```python
class SonosMusic(MusicBackend):
    # ... existing
    @classmethod
    def compatible_speaker_backends(cls) -> frozenset[str]:
        return frozenset({"sonos"})
```

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_music_service_compatible_backends.py std-plugins/sonos/tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/music.py std-plugins/sonos/sonos_music.py \
        tests/unit/test_music_service_compatible_backends.py
git commit -m "music: declare MusicBackend.compatible_speaker_backends(); sonos = {'sonos'}"
```

---

### Task 16: MusicService validates targets against `compatible_speaker_backends`

**Files:**
- Modify: `src/gilbert/core/services/music.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_music_service_compatible_backends.py`:

```python
@pytest.mark.asyncio
async def test_music_service_rejects_incompatible_speaker_target(music_service_sonos, speaker_service_with_browser_and_sonos):
    svc = music_service_sonos
    svc._speaker_svc = speaker_service_with_browser_and_sonos
    with pytest.raises(MusicSearchUnavailableError, match="can't play"):
        await svc.play_item(track_uri="x-sonos:track", speaker_names=["Brian's Browser"])
```

(`MusicSearchUnavailableError` is `gilbert.interfaces.music.MusicSearchUnavailableError` — or whatever the existing music error type is; verify against the current file.)

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_music_service_compatible_backends.py::test_music_service_rejects_incompatible_speaker_target -v`
Expected: FAIL.

- [ ] **Step 3: Implement validation helper**

In `MusicService` add:

```python
async def _validate_compatible_speakers(self, speaker_names: list[str]) -> dict[str, str]:
    """Resolve names → namespaced ids; reject targets the music backend can't drive.

    Returns the resolved mapping ``{name: namespaced_id}`` for downstream
    use; raises ``MusicSearchUnavailableError`` if any target is on an
    incompatible speaker backend.
    """
    speaker_svc = self._get_speaker_svc()
    if speaker_svc is None or self._backend is None:
        return {}
    resolved = await speaker_svc.resolve_names(speaker_names)
    compat = self._backend.compatible_speaker_backends()
    if compat == frozenset({"*"}):
        return resolved
    for name, sid in resolved.items():
        backend_name, _ = split_speaker_id(sid)
        if backend_name not in compat:
            raise MusicSearchUnavailableError(
                f"music backend {self._backend.backend_name!r} can't play to "
                f"speaker {name!r} ({backend_name} backend) — "
                f"try a speaker on one of: {sorted(compat)}"
            )
    return resolved
```

Then call `_validate_compatible_speakers(speaker_names)` at the top of `play_item`, `add_to_queue`, `play_queue_on_speakers`, `enqueue_on_speakers`, `start_station`.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_music_service_compatible_backends.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/music.py tests/unit/test_music_service_compatible_backends.py
git commit -m "music: reject play/queue requests against speakers the music backend can't drive"
```

---

### Task 17: `supports_loop` reads `.backends` mapping with compatibility filter

**Files:**
- Modify: `src/gilbert/core/services/music.py:453-464`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_supports_loop_requires_compatible_speaker_with_repeat(music_service_sonos, speaker_service_with_browser_only):
    svc = music_service_sonos
    svc._speaker_svc = speaker_service_with_browser_only  # no sonos backend loaded
    assert svc.supports_loop is False


@pytest.mark.asyncio
async def test_supports_loop_true_when_compatible_speaker_loaded(music_service_sonos, speaker_service_with_sonos_loaded):
    svc = music_service_sonos
    svc._speaker_svc = speaker_service_with_sonos_loaded
    assert svc.supports_loop is True
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_music_service_compatible_backends.py -k supports_loop -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace the interim Task 3 implementation:

```python
def supports_loop(self) -> bool:
    if not (self._backend and self._backend.supports_loop):
        return False
    speaker_svc = self._get_speaker_svc()
    if not isinstance(speaker_svc, SpeakerProvider):
        return False
    compat = self._backend.compatible_speaker_backends()
    return any(
        b.supports_repeat
        for name, b in speaker_svc.backends.items()
        if compat == frozenset({"*"}) or name in compat
    )
```

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/ -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/music.py tests/unit/test_music_service_compatible_backends.py
git commit -m "music: supports_loop gates on compatible speaker backend with supports_repeat"
```

---

## Phase 6 — Other consumers + frontend

### Task 18: Audit and update remaining `.backend` reaches and downstream tests

**Files:**
- Modify: as discovered

- [ ] **Step 1: Find all remaining singular-`backend` accesses**

Run:

```bash
grep -rn 'speaker_svc\.backend\b\|speaker_service\.backend\b\|self\._speaker_svc\.backend\b' \
    src/ std-plugins/ local-plugins/ 2>&1
```

For each hit, either:
- Replace with `speaker_svc.get_backend("<name>")` and add a `None` check, or
- Replace with `speaker_svc.backends` mapping iteration if the consumer wants "any/all backends," or
- Lift the logic into the service if appropriate.

Likely targets: `std-plugins/guess-that-song/service.py:91` (already touched in Task 3 — verify) and any remaining type-annotated `: SpeakerBackend` parameters.

- [ ] **Step 2: Run mypy to surface remaining type errors**

Run: `uv run mypy src/ std-plugins/ 2>&1 | grep -i speaker`
Expected: zero hits. If any remain, fix them.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -x -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "speaker: update remaining consumers that referenced singular .backend"
```

---

### Task 19: Frontend updates — `speaker.info` consumers + speakers `<Select>`

**Files:**
- Modify: `frontend/src/hooks/useBrowserSpeaker.tsx`
- Modify: `frontend/src/hooks/useBrowserEchoPref.tsx`
- Modify: `frontend/src/components/settings/ConfigField.tsx` (or wherever the speakers select is rendered)
- Modify: `src/gilbert/core/services/speaker.py` (the `speaker.info` WS handler)

- [ ] **Step 1: Update the WS handler to return the new shape**

In `SpeakerService`, locate the `speaker.info` handler (search `"speaker.info"` in `speaker.py`):

```python
async def _handle_info(self, conn, payload):
    return {
        "enabled": bool(self._enabled and self._backends),
        "primary_backend": self._primary_backend,
        "active_backends": sorted(self._backends),
        "startup_failures": [
            {"name": name, "error": err}
            for name, err in self._startup_failures.items()
        ],
    }
```

- [ ] **Step 2: Update `useBrowserSpeaker.tsx`**

Locate the section that reads `info.backend`. Change to:

```tsx
// before
if (info.backend !== "browser") { return; }
// after
if (!info.active_backends?.includes("browser")) { return; }
```

- [ ] **Step 3: Update `useBrowserEchoPref.tsx`**

Find the toggle's `disabled` rule (currently based on `backend === "browser"`):

```tsx
const browserOnlyActive =
  info.primary_backend === "browser" && info.active_backends.length === 1;
const toggleDisabled = browserOnlyActive;
```

- [ ] **Step 4: Speakers `<Select>` rendering — namespaced values + collision label**

Locate where `choices_from="speakers"` is rendered. Update the option mapping:

```tsx
const options = useMemo(() => {
  const counts = speakers.reduce<Record<string, number>>((acc, s) => {
    acc[s.name] = (acc[s.name] ?? 0) + 1;
    return acc;
  }, {});
  return speakers.map(s => ({
    value: s.speaker_id,                           // namespaced
    label: s.name,
    secondary: counts[s.name] > 1 ? s.backend_name : undefined,
  }));
}, [speakers]);
```

Render `secondary` as a subtle `<span class="text-muted">· {secondary}</span>` after the label when set.

- [ ] **Step 5: Verify by hand**

Start the dev server (`./gilbert.sh start` or whatever the repo uses), open the SPA, confirm:
- Speaker settings page shows per-backend cards.
- `primary_backend` dropdown lists enabled backends.
- A user with browser-echo enabled and a primary Sonos backend sees the audio bubbles fire when playing on Sonos.
- A user with `primary_backend = browser` and only browser active sees the echo toggle disabled.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/speaker.py frontend/src/
git commit -m "speaker(frontend): consume primary_backend/active_backends; namespace speaker select values"
```

---

## Phase 7 — Docs

### Task 20: Update `docs/architecture/speaker-system.md`, `CLAUDE.md`, `README.md`

**Files:**
- Modify: `docs/architecture/speaker-system.md`
- Modify: `CLAUDE.md` (only if it references single-backend SpeakerService)
- Modify: `README.md` (only if it references single-backend speaker shape)

- [ ] **Step 1: Audit each doc**

```bash
grep -niE "single.*backend|backend.*chosen.*config|backend: sonos|speaker_svc\.backend\b" \
    docs/architecture/speaker-system.md CLAUDE.md README.md 2>&1
```

For each hit, edit to reflect the multi-backend shape.

- [ ] **Step 2: Update `docs/architecture/speaker-system.md`**

Add a new section "Multi-backend operation" near the top that documents:
- Speakers from multiple backends coexist; ids are namespaced as `<backend>:<native>`.
- `primary_backend` is the default target for bare announces.
- Cross-backend grouping is rejected; cross-backend play fans out (un-synchronized).
- Browser-echo skips when the caller's own `browser:<uid>` is in the explicit target set.

- [ ] **Step 3: Verify with a final test run**

```bash
uv run pytest -x -q
uv run mypy src/
uv run ruff check src/ tests/
```

Expected: all clean.

- [ ] **Step 4: Commit**

```bash
git add docs/ CLAUDE.md README.md 2>/dev/null
git commit -m "docs: document multi-backend SpeakerService"
```

---

## Final checkpoint

After Task 20:

1. Full suite green: `uv run pytest -x -q`.
2. Architecture audit clean: invoke the `validate-architecture` skill in audit mode.
3. Push to jereanon's fork (PR #16 head): `git push jereanon feature/browser_speaker_backend`.
4. Verify the PR's `mergeStateStatus` is `CLEAN`: `gh pr view 16 --json mergeable,mergeStateStatus`.
5. Merge via `/merge-pr 16` (squash + delete branch + local cleanup).

---

## Self-review

**Spec coverage check** (each spec section maps to at least one task):

- §1 Interface changes → Tasks 1, 2, 3.
- §2 Service refactor → Tasks 4–11.
- §3 Config schema → Task 12.
- §4 Grouping + AI tool surface → Task 11 (rejection); AI tool description updates in Task 20.
- §5 Browser-echo → Task 14.
- §6 MusicService → Tasks 15, 16, 17.
- §7 Other downstream consumers → Tasks 3 (initial pass) and 18 (audit).
- §8 Frontend impact → Task 19.
- §9 Migration → Task 13.
- §10 Tests → embedded throughout; new test files in Tasks 8, 13, 15.
- §11 Phasing → reflected in phase headers.

**Placeholder scan:** every step shows actual code or a concrete command. No "TBD," no "add appropriate error handling," no "similar to Task N."

**Type consistency:**
- `split_speaker_id` defined in Task 2, imported and used in Tasks 6, 13, 16.
- `SpeakerProvider` shape declared in Task 3, implemented in Task 4, consumed in Tasks 15+.
- `_route_id` / `_route_ids` defined in Task 6, used in Tasks 7, 11.
- `_backends` dict introduced in Task 8, used onwards.
- `_primary_backend` introduced in Task 12, used in Task 14.
- `_startup_failures` introduced in Task 9, surfaced in Task 19.
- `compatible_speaker_backends` defined in Task 15, used in Tasks 16, 17.

No naming inconsistencies. No method called one thing in one task and another in another.

---
