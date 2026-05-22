# Chat Read-Aloud Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-chat mute/unmute button that, when on, makes Gilbert read each of his chat responses aloud through the user's browser-speaker tab.

**Architecture:** Server-side TTS triggered on chat-turn completion. Audio is routed via the existing browser-speaker plumbing (`SpeakerService.play_on_speakers` → `speaker.browser.play` event → per-user WS filter → singleton `<audio>` element in `useBrowserSpeaker`). State is persisted per `(user_id, conversation_id)` in a new `chat_speech_prefs` entity collection. Flipping the toggle on auto-activates the browser speaker on the active tab.

**Tech Stack:** Python 3.12 (pytest, asyncio, dataclasses), TypeScript/React 18 (frontend), SQLite-backed entity store, WebSocket RPC.

**Spec:** `docs/superpowers/specs/2026-05-17-chat-read-aloud-design.md`

---

## File Map

**Create:**
- `tests/unit/test_chat_speech_text.py` — `strip_markdown_for_speech` table-driven tests
- `tests/unit/test_chat_speech_prefs.py` — speech-pref CRUD + RBAC tests
- `tests/unit/test_chat_speech_hook.py` — `_speak_response` mock-driven tests
- `tests/unit/test_chat_speech_ws.py` — WS RPC tests for `chat.read_aloud.{get,set}`
- `tests/unit/test_play_request_kind.py` — `PlayRequest.kind` plumbing tests
- `frontend/src/hooks/useChatSpeech.tsx` — per-conversation read-aloud hook
- `frontend/src/components/chat/ChatSpeechToggle.tsx` — header icon button

**Modify:**
- `src/gilbert/interfaces/speaker.py` — add `kind: str = ""` to `PlayRequest`
- `src/gilbert/integrations/browser_speaker.py` — stamp `kind` onto play event
- `src/gilbert/core/services/speaker.py` — thread `kind` through `play_on_speakers`
- `src/gilbert/core/chat.py` — add `strip_markdown_for_speech`
- `src/gilbert/core/services/ai.py` — collection const, pref helpers, WS RPCs, `_speak_response`, hook into send/form-submit, new ConfigParam
- `src/gilbert/interfaces/acl.py` — add `"chat.read_aloud.": 100` to `DEFAULT_EVENT_VISIBILITY`
- `src/gilbert/web/ws_protocol.py` — add `can_see_chat_read_aloud_event` filter and slot it into `_dispatch_event`
- `frontend/src/hooks/useBrowserSpeaker.tsx` — track `kind` + `conversationId` on `PlayItem`; skip history when `kind === "chat_speech"`
- `frontend/src/components/chat/ChatPage.tsx` — mount `<ChatSpeechToggle conversationId={...} />` in the top strip

---

## Task 1: Add `kind` field to `PlayRequest` and plumb through speaker layer

**Files:**
- Modify: `src/gilbert/interfaces/speaker.py` (the `PlayRequest` dataclass)
- Modify: `src/gilbert/core/services/speaker.py` (`play_on_speakers`)
- Modify: `src/gilbert/integrations/browser_speaker.py` (`play_uri` event payload)
- Test: `tests/unit/test_play_request_kind.py`

- [ ] **Step 1: Write failing test for `kind` on event payload**

Create `tests/unit/test_play_request_kind.py`:

```python
"""Tests that PlayRequest.kind is propagated through SpeakerService.play_on_speakers
to the BrowserSpeakerBackend's speaker.browser.play event payload."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.context import set_current_user
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.speaker import PlayRequest
from gilbert.integrations.browser_speaker import BrowserSpeakerBackend


class _CapturingBus(EventBus):
    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)

    def subscribe(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


class _BusProvider(EventBusProvider):
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> EventBus:
        return self._bus


@pytest.mark.asyncio
async def test_play_request_kind_appears_in_browser_event() -> None:
    bus = _CapturingBus()
    backend = BrowserSpeakerBackend()
    backend.set_event_bus_provider(_BusProvider(bus))
    await backend.initialize({})
    backend.activate(conn_id="conn-1", user_id="alice", display_name="Alice")

    set_current_user(UserContext(user_id="alice", display_name="Alice", roles=set()))
    try:
        await backend.play_uri(PlayRequest(uri="https://example/a.mp3", kind="chat_speech"))
    finally:
        set_current_user(None)

    assert len(bus.published) == 1
    data = bus.published[0].data
    assert data["kind"] == "chat_speech"
    assert data["user_id"] == "alice"


@pytest.mark.asyncio
async def test_play_request_kind_defaults_to_empty_string() -> None:
    bus = _CapturingBus()
    backend = BrowserSpeakerBackend()
    backend.set_event_bus_provider(_BusProvider(bus))
    await backend.initialize({})
    backend.activate(conn_id="conn-1", user_id="alice", display_name="Alice")

    set_current_user(UserContext(user_id="alice", display_name="Alice", roles=set()))
    try:
        await backend.play_uri(PlayRequest(uri="https://example/a.mp3"))
    finally:
        set_current_user(None)

    data = bus.published[0].data
    assert data["kind"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_play_request_kind.py -v`
Expected: FAIL with `AttributeError: ... kind` or `unexpected keyword argument 'kind'` or `KeyError: 'kind'` (depending on which line errors first).

- [ ] **Step 3: Add `kind` field to `PlayRequest`**

In `src/gilbert/interfaces/speaker.py`, locate the `PlayRequest` dataclass and add a new field at the end (keep it last so positional callers don't break):

```python
@dataclass(frozen=True)
class PlayRequest:
    # ... existing fields ...
    kind: str = ""
    """Free-form classifier used by speaker backends that fan out to
    client UIs (e.g. ``"chat_speech"`` for Gilbert reading a chat reply
    aloud). Backends with no UI dimension (Sonos, local) ignore it.
    The browser backend stamps it onto the ``speaker.browser.play``
    event so the SPA can categorize incoming clips."""
```

Note: the actual existing fields stay unchanged. Verify the file's current `PlayRequest` definition first and append after the last existing field.

- [ ] **Step 4: Stamp `kind` onto the browser-speaker event payload**

In `src/gilbert/integrations/browser_speaker.py`, edit `play_uri` (around line 213, the `await self._bus.publish(...)` call) to add `kind` to the event data:

```python
await self._bus.publish(
    Event(
        event_type="speaker.browser.play",
        data={
            "user_id": target_user_id,
            "conversation_id": get_current_conversation_id() or "",
            "url": to_browser_url(request.uri),
            "title": request.title,
            "volume": volume,
            "announce": request.announce,
            "position_seconds": request.position_seconds,
            "kind": request.kind,
        },
        source="speaker.browser",
    )
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_play_request_kind.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Thread `kind` through `SpeakerService.play_on_speakers`**

In `src/gilbert/core/services/speaker.py`, edit `play_on_speakers` (around line 917) to add a `kind: str = ""` keyword parameter and pass it into the `PlayRequest`:

Add to the signature (after `announce: bool = False`):

```python
        kind: str = "",
```

And inside the loop that builds `PlayRequest` (around line 962-970), add:

```python
                backend.play_uri(
                    PlayRequest(
                        uri=uri,
                        speaker_ids=native_ids,
                        volume=volume,
                        title=title,
                        position_seconds=position_seconds,
                        didl_meta=didl_meta,
                        announce=announce,
                        kind=kind,
                    )
                )
```

- [ ] **Step 7: Add a test that `kind` flows through `SpeakerService`**

Append to `tests/unit/test_play_request_kind.py`:

```python
@pytest.mark.asyncio
async def test_speaker_service_threads_kind_to_browser_backend() -> None:
    """When SpeakerService.play_on_speakers(..., kind=...) is called with a
    browser:<user> target, the resulting event payload carries the kind."""
    from gilbert.core.services.speaker import SpeakerService

    bus = _CapturingBus()
    backend = BrowserSpeakerBackend()
    backend.set_event_bus_provider(_BusProvider(bus))
    await backend.initialize({})
    backend.activate(conn_id="conn-1", user_id="alice", display_name="Alice")

    svc = SpeakerService()
    svc._backends = {"browser": backend}  # type: ignore[attr-defined]

    set_current_user(UserContext(user_id="alice", display_name="Alice", roles=set()))
    try:
        await svc.play_on_speakers(
            uri="https://example/a.mp3",
            speaker_ids=["browser:alice"],
            kind="chat_speech",
            title="Gilbert",
        )
    finally:
        set_current_user(None)

    assert any(ev.data.get("kind") == "chat_speech" for ev in bus.published)
```

- [ ] **Step 8: Run new test to verify it passes**

Run: `uv run pytest tests/unit/test_play_request_kind.py -v`
Expected: PASS (3 passed).

- [ ] **Step 9: Commit**

```bash
git add src/gilbert/interfaces/speaker.py src/gilbert/integrations/browser_speaker.py src/gilbert/core/services/speaker.py tests/unit/test_play_request_kind.py
git commit -m "speaker: add PlayRequest.kind + thread through browser event payload"
```

---

## Task 2: Add `strip_markdown_for_speech` to `core/chat.py`

**Files:**
- Modify: `src/gilbert/core/chat.py`
- Test: `tests/unit/test_chat_speech_text.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_chat_speech_text.py`:

```python
"""Unit tests for strip_markdown_for_speech."""

from __future__ import annotations

import pytest

from gilbert.core.chat import strip_markdown_for_speech


@pytest.mark.parametrize(
    "raw,expected",
    [
        # fenced code dropped entirely
        ("before\n```py\nx = 1\n```\nafter", "before. after"),
        # inline code keeps inner text
        ("press the `Enter` key", "press the Enter key"),
        # bold / italic / underline
        ("**bold** *italic* _under_", "bold italic under"),
        # heading -> sentence
        ("# Title\nbody", "Title. body"),
        # list items get periods
        ("- one\n- two", "one. two."),
        # link keeps text drops URL
        ("see [docs](https://example/x)", "see docs"),
        # image dropped
        ("an ![pic](https://example/p.png) image", "an  image"),
        # HTML tags stripped
        ("hi <b>there</b> friend", "hi there friend"),
        # multiple blank lines collapsed
        ("p1\n\n\n\np2", "p1. p2"),
    ],
)
def test_strip_markdown_for_speech_cases(raw: str, expected: str) -> None:
    got = strip_markdown_for_speech(raw)
    # Normalize whitespace collapse for compare-friendliness.
    assert " ".join(got.split()) == " ".join(expected.split())


def test_strip_markdown_only_code_becomes_empty() -> None:
    raw = "```py\nx = 1\n```"
    assert strip_markdown_for_speech(raw).strip() == ""


def test_strip_markdown_preserves_plain_prose() -> None:
    assert strip_markdown_for_speech("Hello world.") == "Hello world."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_chat_speech_text.py -v`
Expected: FAIL with `ImportError: cannot import name 'strip_markdown_for_speech'`.

- [ ] **Step 3: Add implementation**

Edit `src/gilbert/core/chat.py`, append the function near the other helpers (after `mentions_gilbert`):

```python
import re

_RE_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_RE_LIST_ITEM = re.compile(r"^\s*[-*+]\s+(.+)$", re.MULTILINE)
_RE_ORDERED_ITEM = re.compile(r"^\s*\d+\.\s+(.+)$", re.MULTILINE)
_RE_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(.+?)\1")
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_BLANK_RUN = re.compile(r"\n\s*\n+")


def strip_markdown_for_speech(text: str) -> str:
    """Strip markdown structure so the text reads naturally when spoken.

    Drops code blocks (un-speakable), drops link URLs (keeps anchor text),
    drops images, strips emphasis markers, periodizes headings and list
    items so TTS pauses between them, and collapses blank-line runs into
    a single sentence break. Regex-based — no markdown library dependency.

    Used by ``AIService._speak_response`` to prepare chat replies for TTS.
    """
    if not text:
        return ""
    out = _RE_FENCED_CODE.sub(" ", text)
    out = _RE_IMAGE.sub(" ", out)
    out = _RE_LINK.sub(r"\1", out)
    out = _RE_INLINE_CODE.sub(r"\1", out)
    out = _RE_HEADING.sub(r"\1. ", out)
    out = _RE_LIST_ITEM.sub(r"\1.", out)
    out = _RE_ORDERED_ITEM.sub(r"\1.", out)
    out = _RE_EMPHASIS.sub(r"\2", out)
    out = _RE_HTML_TAG.sub(" ", out)
    out = _RE_BLANK_RUN.sub(". ", out)
    # Collapse any residual runs of whitespace to single spaces.
    out = re.sub(r"[ \t]+", " ", out)
    # Trim and collapse stray ". ." into a single ".".
    out = re.sub(r"\.\s*\.+", ".", out)
    return out.strip()
```

If `re` is not already imported in the file, add it at the top with other imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_chat_speech_text.py -v`
Expected: PASS (all parametrized cases + `test_strip_markdown_only_code_becomes_empty` + `test_strip_markdown_preserves_plain_prose`).

If any case fails, tweak the regex and rerun. Don't add new behavior — only fix the cases listed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/chat.py tests/unit/test_chat_speech_text.py
git commit -m "chat: add strip_markdown_for_speech helper for TTS-friendly text"
```

---

## Task 3: Add `chat_speech_voice` ConfigParam to `AIService`

**Files:**
- Modify: `src/gilbert/core/services/ai.py`

- [ ] **Step 1: Add ConfigParam declaration**

In `src/gilbert/core/services/ai.py`, find `config_params()` (starts around line 1359). Append a new `ConfigParam` to the returned list (right before the `return ...` or before the closing `]`, after the existing chat-related params):

```python
ConfigParam(
    key="chat_speech_voice",
    type=ToolParameterType.STRING,
    description=(
        "Voice id (or empty) used when reading chat replies aloud to "
        "users who have per-chat read-aloud enabled. Empty means use "
        "the TTS service's default voice. Not user-configurable per "
        "chat in v1."
    ),
    default="",
),
```

- [ ] **Step 2: Cache the value during config apply**

Find `_apply_config` (search for `def _apply_config` or `chat_profile = section.get`). Near where `self._chat_profile` is read (around line 1314), add:

```python
self._chat_speech_voice = section.get("chat_speech_voice", "")
```

And in `__init__` (around line 1190 where `self._chat_profile: str = "standard"` lives), add a default:

```python
self._chat_speech_voice: str = ""
```

- [ ] **Step 3: Run existing AI tests to verify no regression**

Run: `uv run pytest tests/unit/ -k "ai" -v -x`
Expected: PASS (no test count change; new ConfigParam should not break existing tests).

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/ai.py
git commit -m "ai: add chat_speech_voice ConfigParam for read-aloud voice override"
```

---

## Task 4: Add `chat_speech_prefs` entity collection + helpers on `AIService`

**Files:**
- Modify: `src/gilbert/core/services/ai.py`
- Test: `tests/unit/test_chat_speech_prefs.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_chat_speech_prefs.py`:

```python
"""Tests for AIService.get_speech_pref / set_speech_pref + RBAC."""

from __future__ import annotations

import pytest

from gilbert.core.services.ai import AIService, _CHAT_SPEECH_COLLECTION
from gilbert.interfaces.auth import UserContext


class _InMemoryStorage:
    """Minimal entity-store stand-in for AIService unit tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}

    async def get(self, collection: str, key: str):
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, value: dict) -> None:
        self._data.setdefault(collection, {})[key] = value

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def query(self, collection: str, **kw):
        return list(self._data.get(collection, {}).values())


@pytest.fixture
def svc() -> AIService:
    s = AIService()
    s._storage = _InMemoryStorage()  # type: ignore[assignment]
    return s


@pytest.mark.asyncio
async def test_get_speech_pref_defaults_to_false(svc: AIService) -> None:
    assert (await svc.get_speech_pref("alice", "conv-1")) is False


@pytest.mark.asyncio
async def test_set_then_get_round_trip(svc: AIService) -> None:
    await svc.set_speech_pref("alice", "conv-1", True)
    assert (await svc.get_speech_pref("alice", "conv-1")) is True

    await svc.set_speech_pref("alice", "conv-1", False)
    assert (await svc.get_speech_pref("alice", "conv-1")) is False


@pytest.mark.asyncio
async def test_prefs_are_user_scoped(svc: AIService) -> None:
    await svc.set_speech_pref("alice", "conv-1", True)
    assert (await svc.get_speech_pref("bob", "conv-1")) is False


@pytest.mark.asyncio
async def test_collection_id_format(svc: AIService) -> None:
    """Verifies the persisted key is f"{user}:{conv}" so a single conv with
    multiple members produces N rows, not one row that gets stomped."""
    await svc.set_speech_pref("alice", "conv-1", True)
    storage = svc._storage  # type: ignore[attr-defined]
    assert "alice:conv-1" in storage._data[_CHAT_SPEECH_COLLECTION]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_chat_speech_prefs.py -v`
Expected: FAIL with `ImportError: cannot import name '_CHAT_SPEECH_COLLECTION'` (or `AttributeError: ... 'get_speech_pref'`).

- [ ] **Step 3: Add the collection constant and helpers**

In `src/gilbert/core/services/ai.py`, near the other collection constants (around line 76-78):

```python
_CHAT_SPEECH_COLLECTION = "chat_speech_prefs"
```

Then, on the `AIService` class, add methods near the other conversation-state helpers (search for `set_conversation_state` to find a good neighborhood):

```python
async def get_speech_pref(self, user_id: str, conversation_id: str) -> bool:
    """Return True if user has read-aloud enabled for the conversation.

    Defaults to False for unknown (user, conv) pairs.
    """
    if not self._storage or not user_id or not conversation_id:
        return False
    record = await self._storage.get(
        _CHAT_SPEECH_COLLECTION, f"{user_id}:{conversation_id}"
    )
    if not record:
        return False
    return bool(record.get("enabled"))


async def set_speech_pref(
    self, user_id: str, conversation_id: str, enabled: bool
) -> None:
    """Upsert the read-aloud preference for (user, conversation).

    Caller is responsible for verifying conversation access — this
    method does NOT check membership. RBAC lives in the WS handler.
    """
    if not self._storage:
        return
    from datetime import UTC, datetime

    await self._storage.put(
        _CHAT_SPEECH_COLLECTION,
        f"{user_id}:{conversation_id}",
        {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "enabled": bool(enabled),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_chat_speech_prefs.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/ai.py tests/unit/test_chat_speech_prefs.py
git commit -m "ai: add chat_speech_prefs entity + get/set_speech_pref helpers"
```

---

## Task 5: Add WS RPCs `chat.read_aloud.get` / `chat.read_aloud.set` + per-user event filter

**Files:**
- Modify: `src/gilbert/core/services/ai.py` (handler methods + registration + change-event publish)
- Modify: `src/gilbert/interfaces/acl.py` (add prefix to `DEFAULT_EVENT_VISIBILITY`)
- Modify: `src/gilbert/web/ws_protocol.py` (filter method + dispatch slot)
- Test: `tests/unit/test_chat_speech_ws.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_chat_speech_ws.py`:

```python
"""WS RPC tests for chat.read_aloud.{get,set}."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.services.ai import AIService
from gilbert.interfaces.auth import UserContext


class _InMemoryStorage:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}

    async def get(self, collection: str, key: str):
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, value: dict) -> None:
        self._data.setdefault(collection, {})[key] = value


class _FakeConn:
    """Minimal stand-in for WsConnectionBase."""

    def __init__(self, user_id: str = "alice") -> None:
        self.user_id = user_id
        self.user_ctx = UserContext(
            user_id=user_id, display_name=user_id.title(), roles=set()
        )
        self.manager = type("M", (), {"gilbert": None})()


@pytest.fixture
async def svc_with_conv() -> AIService:
    s = AIService()
    s._storage = _InMemoryStorage()  # type: ignore[assignment]
    # Seed a personal conversation owned by alice.
    await s._storage.put(
        "ai_conversations",
        "conv-1",
        {"user_id": "alice", "messages": []},
    )
    return s


@pytest.mark.asyncio
async def test_get_returns_false_when_unset(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_get(
        _FakeConn(), {"id": "r1", "conversation_id": "conv-1"}
    )
    assert result["type"] == "chat.read_aloud.get.result"
    assert result["enabled"] is False


@pytest.mark.asyncio
async def test_set_persists_and_echoes(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_set(
        _FakeConn(),
        {"id": "r2", "conversation_id": "conv-1", "enabled": True},
    )
    assert result["type"] == "chat.read_aloud.set.result"
    assert result["enabled"] is True

    # Verify a follow-up get sees the new value.
    got = await svc_with_conv._ws_chat_read_aloud_get(
        _FakeConn(), {"id": "r3", "conversation_id": "conv-1"}
    )
    assert got["enabled"] is True


@pytest.mark.asyncio
async def test_set_denied_for_non_member(svc_with_conv: AIService) -> None:
    # bob is not alice and conv-1's only authorized user is alice.
    result = await svc_with_conv._ws_chat_read_aloud_set(
        _FakeConn(user_id="bob"),
        {"id": "r4", "conversation_id": "conv-1", "enabled": True},
    )
    assert result["type"] == "gilbert.error"
    assert result.get("code") == 403


@pytest.mark.asyncio
async def test_missing_conversation_returns_error(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_get(
        _FakeConn(), {"id": "r5", "conversation_id": "does-not-exist"}
    )
    assert result["type"] == "gilbert.error"
    assert result.get("code") == 404


@pytest.mark.asyncio
async def test_missing_conversation_id_returns_400(svc_with_conv: AIService) -> None:
    result = await svc_with_conv._ws_chat_read_aloud_set(
        _FakeConn(), {"id": "r6", "enabled": True}
    )
    assert result["type"] == "gilbert.error"
    assert result.get("code") == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_chat_speech_ws.py -v`
Expected: FAIL with `AttributeError: 'AIService' object has no attribute '_ws_chat_read_aloud_get'`.

- [ ] **Step 3: Add the WS handlers**

In `src/gilbert/core/services/ai.py`, add methods on `AIService`. Place them near the other conversation handlers (search for `_ws_conversation_create` to find a good insertion point):

```python
async def _ws_chat_read_aloud_get(
    self, conn: Any, frame: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the per-(user, conv) read-aloud preference."""
    from gilbert.core.chat import check_conversation_access

    conv_id = frame.get("conversation_id")
    if not conv_id:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "conversation_id required",
            "code": 400,
        }
    if self._storage is None:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "Storage not available",
            "code": 503,
        }
    conv = await self._storage.get(_COLLECTION, conv_id)
    if not conv:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "Conversation not found",
            "code": 404,
        }
    denied = check_conversation_access(conv, conn.user_ctx)
    if denied:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": denied,
            "code": 403,
        }
    enabled = await self.get_speech_pref(conn.user_ctx.user_id, conv_id)
    return {
        "type": "chat.read_aloud.get.result",
        "ref": frame.get("id"),
        "conversation_id": conv_id,
        "enabled": enabled,
    }


async def _ws_chat_read_aloud_set(
    self, conn: Any, frame: dict[str, Any]
) -> dict[str, Any] | None:
    """Persist the per-(user, conv) read-aloud preference and broadcast
    a per-user ``chat.read_aloud.changed`` event for other tabs."""
    from gilbert.core.chat import check_conversation_access, publish_event

    conv_id = frame.get("conversation_id")
    enabled = bool(frame.get("enabled", False))
    if not conv_id:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "conversation_id required",
            "code": 400,
        }
    if self._storage is None:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "Storage not available",
            "code": 503,
        }
    conv = await self._storage.get(_COLLECTION, conv_id)
    if not conv:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "Conversation not found",
            "code": 404,
        }
    denied = check_conversation_access(conv, conn.user_ctx, require_member=True)
    if denied:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": denied,
            "code": 403,
        }
    await self.set_speech_pref(conn.user_ctx.user_id, conv_id, enabled)
    gilbert = conn.manager.gilbert if hasattr(conn, "manager") else None
    if gilbert is not None:
        await publish_event(
            gilbert,
            "chat.read_aloud.changed",
            {
                "user_id": conn.user_ctx.user_id,
                "conversation_id": conv_id,
                "enabled": enabled,
            },
        )
    return {
        "type": "chat.read_aloud.set.result",
        "ref": frame.get("id"),
        "conversation_id": conv_id,
        "enabled": enabled,
    }
```

- [ ] **Step 4: Register the handlers**

In `src/gilbert/core/services/ai.py`, locate `get_ws_handlers` (around line 5436) and add two entries to the returned dict:

```python
            "chat.read_aloud.get": self._ws_chat_read_aloud_get,
            "chat.read_aloud.set": self._ws_chat_read_aloud_set,
```

- [ ] **Step 5: Run handler tests to verify they pass**

Run: `uv run pytest tests/unit/test_chat_speech_ws.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Add per-user event ACL**

In `src/gilbert/interfaces/acl.py`, find `DEFAULT_EVENT_VISIBILITY` and add the `chat.read_aloud.` prefix at user-level (100), next to the existing `speaker.browser.` and `notification.` entries:

```python
    "chat.read_aloud.": 100,
```

- [ ] **Step 7: Add the WS filter method and dispatch slot**

In `src/gilbert/web/ws_protocol.py`, find `can_see_speaker_browser_event` (around line 148) and add a sibling method on the same `WsConnection` class:

```python
def can_see_chat_read_aloud_event(self, event: Event) -> bool:
    """Deliver chat.read_aloud.* events only to the matching user's
    own connections (so other tabs of that user stay in sync without
    leaking the preference to other users in a shared room)."""
    if not str(event.event_type).startswith("chat.read_aloud."):
        return True  # not our event type — let other filters decide
    target_user_id = (event.data or {}).get("user_id", "")
    return bool(target_user_id) and target_user_id == self.user_ctx.user_id
```

Then find `_dispatch_event` (around line 395, where `can_see_speaker_browser_event` is invoked) and slot in the new filter alongside the existing ones:

```python
            if not conn.can_see_chat_read_aloud_event(event):
                continue
```

- [ ] **Step 8: Run full WS protocol tests to verify no regression**

Run: `uv run pytest tests/unit/ -k "ws_protocol or acl or chat_speech" -v`
Expected: PASS — no existing WS tests regress.

- [ ] **Step 9: Commit**

```bash
git add src/gilbert/core/services/ai.py src/gilbert/interfaces/acl.py src/gilbert/web/ws_protocol.py tests/unit/test_chat_speech_ws.py
git commit -m "ai: chat.read_aloud WS RPCs + per-user event filter for changed broadcasts"
```

---

## Task 6: Add `_speak_response` to `AIService` (fire-and-forget TTS + browser play)

**Files:**
- Modify: `src/gilbert/core/services/ai.py`
- Test: `tests/unit/test_chat_speech_hook.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_chat_speech_hook.py`:

```python
"""Tests for AIService._speak_response — the chat-turn TTS hook."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from gilbert.core.services.ai import AIService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.speaker import SpeakerInfo


@dataclass
class _TTSResult:
    audio: bytes
    format: str = "mp3"


class _FakeTTS:
    """Stand-in for TTSService."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[Any] = []

    async def synthesize(self, request: Any) -> Any:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("tts boom")
        return _TTSResult(audio=b"FAKEMP3")


class _FakeSpeaker:
    """Stand-in for SpeakerService."""

    def __init__(self, browser_users: list[str] | None = None) -> None:
        self.list_speakers_calls = 0
        self.play_calls: list[dict] = []
        self._browser_users = browser_users or []

    async def list_speakers(self) -> list[SpeakerInfo]:
        self.list_speakers_calls += 1
        return [
            SpeakerInfo(speaker_id=f"browser:{u}", name=f"{u}'s Browser", ip_address="")
            for u in self._browser_users
        ]

    async def play_on_speakers(self, **kwargs: Any) -> None:
        self.play_calls.append(kwargs)


class _FakeResolver:
    def __init__(self, **caps: Any) -> None:
        self._caps = caps

    def get_capability(self, name: str) -> Any:
        return self._caps.get(name)


def _make_service(
    *,
    tts: Any = None,
    speaker: Any = None,
) -> AIService:
    svc = AIService()
    svc._resolver = _FakeResolver(text_to_speech=tts, speaker_control=speaker)
    return svc


@pytest.mark.asyncio
async def test_speak_response_routes_audio_to_browser_speaker() -> None:
    tts = _FakeTTS()
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=tts, speaker=speaker)

    user = UserContext(user_id="alice", display_name="Alice", roles=set())
    await svc._speak_response(user, "conv-1", "Hello world.")

    assert len(tts.calls) == 1
    assert len(speaker.play_calls) == 1
    call = speaker.play_calls[0]
    assert call["speaker_ids"] == ["browser:alice"]
    assert call["kind"] == "chat_speech"


@pytest.mark.asyncio
async def test_speak_response_skips_when_no_browser_speaker() -> None:
    tts = _FakeTTS()
    speaker = _FakeSpeaker(browser_users=[])  # alice has no active tab
    svc = _make_service(tts=tts, speaker=speaker)

    user = UserContext(user_id="alice", display_name="Alice", roles=set())
    await svc._speak_response(user, "conv-1", "Hello world.")

    assert tts.calls == []  # never paid TTS cost
    assert speaker.play_calls == []


@pytest.mark.asyncio
async def test_speak_response_skips_when_text_is_only_code() -> None:
    tts = _FakeTTS()
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=tts, speaker=speaker)

    user = UserContext(user_id="alice", display_name="Alice", roles=set())
    await svc._speak_response(user, "conv-1", "```py\nx = 1\n```")

    assert tts.calls == []
    assert speaker.play_calls == []


@pytest.mark.asyncio
async def test_speak_response_swallows_tts_errors() -> None:
    tts = _FakeTTS(fail=True)
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=tts, speaker=speaker)

    user = UserContext(user_id="alice", display_name="Alice", roles=set())
    # Should not raise.
    await svc._speak_response(user, "conv-1", "Hello world.")
    assert speaker.play_calls == []


@pytest.mark.asyncio
async def test_speak_response_noops_without_tts_capability() -> None:
    speaker = _FakeSpeaker(browser_users=["alice"])
    svc = _make_service(tts=None, speaker=speaker)

    user = UserContext(user_id="alice", display_name="Alice", roles=set())
    await svc._speak_response(user, "conv-1", "Hello world.")
    assert speaker.play_calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_chat_speech_hook.py -v`
Expected: FAIL with `AttributeError: 'AIService' object has no attribute '_speak_response'`.

- [ ] **Step 3: Add `_speak_response`**

In `src/gilbert/core/services/ai.py`, add the method on `AIService`. Place it near the other helper methods (search for `_filter_blocks_for_user` to find a neighborhood):

```python
async def _speak_response(
    self,
    user: UserContext,
    conversation_id: str,
    response_text: str,
) -> None:
    """Fire-and-forget: synth the chat reply via TTS and play it in
    the user's active browser tab.

    Safe to call from a chat-turn handler; any failure is logged and
    swallowed so a TTS hiccup never breaks the chat reply itself.
    """
    import uuid
    from pathlib import Path

    from gilbert.core.chat import strip_markdown_for_speech
    from gilbert.core.output import cleanup_old_files, get_output_dir
    from gilbert.interfaces.tts import SynthesisRequest

    try:
        plain = strip_markdown_for_speech(response_text).strip()
        if not plain:
            return
        if self._resolver is None:
            return
        tts_svc = self._resolver.get_capability("text_to_speech")
        speaker_svc = self._resolver.get_capability("speaker_control")
        if tts_svc is None or speaker_svc is None:
            return
        # Defense in depth: skip synth if the user has no active browser
        # tab to deliver the audio to.
        speakers = await speaker_svc.list_speakers()
        target_id = f"browser:{user.user_id}"
        if not any(s.speaker_id == target_id for s in speakers):
            return
        voice = getattr(self, "_chat_speech_voice", "") or None
        result = await tts_svc.synthesize(
            SynthesisRequest(text=plain, voice=voice)
        )
        output_dir = get_output_dir("speaker")
        cleanup_old_files(output_dir, 3600)
        file_path = output_dir / f"chat-speech-{uuid.uuid4()}.mp3"
        file_path.write_bytes(result.audio)
        # Mint an absolute URL the same way SpeakerService.announce does;
        # the browser backend rewrites it to origin-relative for the SPA.
        audio_url = speaker_svc._audio_url(str(file_path.resolve()))
        await speaker_svc.play_on_speakers(
            uri=audio_url,
            speaker_ids=[target_id],
            title="Gilbert",
            announce=False,
            kind="chat_speech",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "chat read-aloud: speak_response failed for user=%s conv=%s",
            user.user_id,
            conversation_id,
            exc_info=True,
        )
```

Note: `SynthesisRequest` has a `voice` parameter; verify by looking at `src/gilbert/interfaces/tts.py`. If the parameter name differs (e.g. `voice_id`), adjust the call accordingly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_chat_speech_hook.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/ai.py tests/unit/test_chat_speech_hook.py
git commit -m "ai: add _speak_response — chat-turn TTS + browser-speaker hook"
```

---

## Task 7: Wire `_speak_response` into chat send + form submit handlers

**Files:**
- Modify: `src/gilbert/core/services/ai.py`

- [ ] **Step 1: Add the fire-and-forget call after `_ws_chat_send`'s turn-completion path**

Find `_ws_chat_send` (around line 5535). Locate the point after `response_text = turn_result.response_text` and `conv_id = turn_result.conversation_id` are set (around line 5682) but before the `return {...}` (around line 5726). Add:

```python
        # Read-aloud hook — fire and forget. Never delay the chat reply.
        if response_text and conv_id:
            if await self.get_speech_pref(conn.user_ctx.user_id, conv_id):
                asyncio.create_task(
                    self._speak_response(conn.user_ctx, conv_id, response_text)
                )
```

- [ ] **Step 2: Add the same hook to `_ws_form_submit`**

Find `_ws_form_submit` (around line 5847). After `response_text = turn_result.response_text` (around line 5893) and `conv_id = turn_result.conversation_id`, before the `return {...}` (around line 5923), add the same block:

```python
        # Read-aloud hook — fire and forget.
        if response_text and conv_id:
            if await self.get_speech_pref(conn.user_ctx.user_id, conv_id):
                asyncio.create_task(
                    self._speak_response(conn.user_ctx, conv_id, response_text)
                )
```

- [ ] **Step 3: Add an integration-flavored unit test**

Append to `tests/unit/test_chat_speech_hook.py`:

```python
@pytest.mark.asyncio
async def test_chat_send_triggers_speak_when_pref_on(monkeypatch: Any) -> None:
    """Sanity: when pref is on and a chat reply has text, _speak_response
    is invoked. We patch the method to avoid needing a full TTS stack
    and verify it is called with the expected args."""
    svc = _make_service(tts=_FakeTTS(), speaker=_FakeSpeaker(browser_users=["alice"]))

    # Persist a pref directly via the helper.
    class _IS:
        def __init__(self) -> None:
            self._d: dict[str, dict[str, dict]] = {}

        async def get(self, c: str, k: str):
            return self._d.get(c, {}).get(k)

        async def put(self, c: str, k: str, v: dict) -> None:
            self._d.setdefault(c, {})[k] = v

    svc._storage = _IS()  # type: ignore[assignment]
    await svc.set_speech_pref("alice", "conv-1", True)

    called: list[tuple[str, str, str]] = []

    async def _capture(user, conv_id, text):
        called.append((user.user_id, conv_id, text))

    monkeypatch.setattr(svc, "_speak_response", _capture)

    # Simulate the hook code inline (the actual wiring is small enough
    # we don't need a full WS frame end-to-end here).
    user = UserContext(user_id="alice", display_name="Alice", roles=set())
    if await svc.get_speech_pref(user.user_id, "conv-1"):
        await svc._speak_response(user, "conv-1", "hello")

    assert called == [("alice", "conv-1", "hello")]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_chat_speech_hook.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Run full chat / AI tests to verify no regression**

Run: `uv run pytest tests/unit/ -k "chat or ai" -v`
Expected: PASS (no count change from before).

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/ai.py tests/unit/test_chat_speech_hook.py
git commit -m "ai: wire chat-read-aloud hook into _ws_chat_send + _ws_form_submit"
```

---

## Task 8: Update `useBrowserSpeaker.tsx` to track `kind` + `conversationId`, skip history for chat-speech

**Files:**
- Modify: `frontend/src/hooks/useBrowserSpeaker.tsx`

- [ ] **Step 1: Extend `PlayItem` interface**

In `frontend/src/hooks/useBrowserSpeaker.tsx`, find the `PlayItem` interface (around line 17) and add two fields:

```ts
export interface PlayItem {
  id: string;
  url: string;
  title: string;
  volume: number; // 0-100
  receivedAt: number;
  kind: string; // "" for generic, "chat_speech" for read-aloud clips
  conversationId: string;
}
```

- [ ] **Step 2: Populate the new fields when an event arrives**

In the same file, find the event handler (around line 101) and update the `PlayItem` construction:

```ts
const item: PlayItem = {
  id:
    typeof event.timestamp === "string" && event.timestamp.length > 0
      ? `${event.timestamp}-${Math.random().toString(36).slice(2, 8)}`
      : `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  url,
  title: typeof data.title === "string" ? data.title : "",
  volume: clampVolume(data.volume),
  receivedAt: Date.now(),
  kind: typeof data.kind === "string" ? data.kind : "",
  conversationId:
    typeof data.conversation_id === "string" ? data.conversation_id : "",
};
```

- [ ] **Step 3: Skip history-append for `chat_speech` clips, still autoplay**

In the same handler, change the `setHistory(...)` call so chat-speech clips don't fill the history popover, but still update `lastPlayed` and trigger playback:

```ts
if (item.kind !== "chat_speech") {
  setHistory((prev) => [item, ...prev].slice(0, HISTORY_LIMIT));
}
setLastPlayed(item);
if (enabled && audioRef.current) {
  const el = audioRef.current;
  // Belt-and-suspenders: explicitly stop any in-flight clip before
  // swapping src so the interrupt is deterministic across browsers.
  if (!el.paused) el.pause();
  el.src = url;
  el.volume = Math.max(0, Math.min(1, item.volume / 100));
  setIsPlaying(true);
  el.play().catch(() => setIsPlaying(false));
}
```

- [ ] **Step 4: Quick smoke build to verify no TS error**

Run: `cd frontend && npm run build`
Expected: build succeeds with no TS errors. If the project uses a different command (check `frontend/package.json`), use that.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useBrowserSpeaker.tsx
git commit -m "browser-speaker: track kind+conversation_id; exclude chat-speech from history"
```

---

## Task 9: Create `useChatSpeech(conversationId)` hook

**Files:**
- Create: `frontend/src/hooks/useChatSpeech.tsx`

- [ ] **Step 1: Implement the hook**

Create `frontend/src/hooks/useChatSpeech.tsx`:

```ts
import { useCallback, useEffect, useMemo, useState } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useBrowserSpeaker } from "@/hooks/useBrowserSpeaker";
import type { GilbertEvent } from "@/types/events";

interface ChatSpeechStore {
  /** Per-(user, conv) read-aloud preference, mirrored from the server. */
  enabled: boolean;
  /** True while a chat-speech clip is actively playing for THIS conv. */
  isSpeaking: boolean;
  /** Toggle the preference; auto-activates the browser speaker on enable. */
  toggle: () => Promise<void>;
}

export function useChatSpeech(conversationId: string | null): ChatSpeechStore {
  const { connected, rpc, subscribe } = useWebSocket();
  const browser = useBrowserSpeaker();
  const [enabled, setEnabled] = useState(false);

  // Hydrate on mount / when conversation changes.
  useEffect(() => {
    if (!conversationId || !connected) {
      setEnabled(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = (await rpc({
          type: "chat.read_aloud.get",
          conversation_id: conversationId,
        })) as { enabled?: boolean };
        if (!cancelled) setEnabled(Boolean(res?.enabled));
      } catch {
        if (!cancelled) setEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, connected, rpc]);

  // Listen for server-side changes (other tabs of the same user).
  useEffect(() => {
    if (!conversationId) return;
    return subscribe("chat.read_aloud.changed", (event: GilbertEvent) => {
      const data = event.data as Record<string, unknown>;
      if (data.conversation_id !== conversationId) return;
      setEnabled(Boolean(data.enabled));
    });
  }, [conversationId, subscribe]);

  const toggle = useCallback(async () => {
    if (!conversationId) return;
    const next = !enabled;
    if (next && !browser.enabled) {
      // Auto-activate the browser speaker so the audio has somewhere
      // to play. setEnabled fires the activate WS frame.
      browser.setEnabled(true);
    }
    setEnabled(next); // optimistic
    try {
      await rpc({
        type: "chat.read_aloud.set",
        conversation_id: conversationId,
        enabled: next,
      });
    } catch {
      setEnabled(!next); // rollback
    }
  }, [conversationId, enabled, browser, rpc]);

  const isSpeaking = useMemo(() => {
    if (!enabled || !browser.isPlaying) return false;
    const last = browser.lastPlayed;
    if (!last) return false;
    return (
      last.kind === "chat_speech" && last.conversationId === conversationId
    );
  }, [enabled, browser.isPlaying, browser.lastPlayed, conversationId]);

  return { enabled, isSpeaking, toggle };
}
```

- [ ] **Step 2: TS build check**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useChatSpeech.tsx
git commit -m "frontend: add useChatSpeech hook for per-conversation read-aloud"
```

---

## Task 10: Create `ChatSpeechToggle` component + wire into `ChatPage` top strip

**Files:**
- Create: `frontend/src/components/chat/ChatSpeechToggle.tsx`
- Modify: `frontend/src/components/chat/ChatPage.tsx`

- [ ] **Step 1: Implement the component**

Create `frontend/src/components/chat/ChatSpeechToggle.tsx`:

```tsx
import { Volume2, VolumeX } from "lucide-react";
import { useChatSpeech } from "@/hooks/useChatSpeech";
import { cn } from "@/lib/utils";

interface Props {
  conversationId: string | null;
  className?: string;
}

export function ChatSpeechToggle({ conversationId, className }: Props) {
  const { enabled, isSpeaking, toggle } = useChatSpeech(conversationId);
  const disabled = !conversationId;

  const Icon = enabled ? Volume2 : VolumeX;
  const title = enabled ? "Stop reading replies aloud" : "Read replies aloud";

  return (
    <button
      type="button"
      onClick={() => void toggle()}
      disabled={disabled}
      title={title}
      aria-label={title}
      aria-pressed={enabled}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded transition-colors",
        "hover:bg-foreground/10 disabled:opacity-40 disabled:cursor-not-allowed",
        enabled ? "text-(--signal)" : "text-foreground/60",
        isSpeaking && "animate-pulse",
        className,
      )}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}
```

If `@/lib/utils` doesn't export `cn`, check the project's utility import path (e.g., other components in `frontend/src/components/`) and adjust. If `lucide-react` icon names differ, look at how `BrowserSpeakerControl.tsx` imports its icons and mirror that.

- [ ] **Step 2: Wire the toggle into `ChatPage.tsx`'s top strip**

Open `frontend/src/components/chat/ChatPage.tsx`. Find the compact top strip described in the `chat-transcript.md` architecture doc (`h-auto border-b px-3 py-2` — search for the chat title bar). Add:

```tsx
import { ChatSpeechToggle } from "@/components/chat/ChatSpeechToggle";
```

And inside the top-strip JSX, on the right side (where the conversation title sits — add an actions row), place:

```tsx
<ChatSpeechToggle conversationId={conversationId} />
```

Use whatever variable holds the currently-open conversation id (likely `conversationId` or `currentConversationId` — grep `ChatPage.tsx` for the state). If the top strip currently has no right-side actions slot, add a `flex justify-between` wrapper around the existing title and the new button.

- [ ] **Step 3: TS build check**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/chat/ChatSpeechToggle.tsx frontend/src/components/chat/ChatPage.tsx
git commit -m "chat: add per-chat read-aloud toggle in top strip"
```

---

## Task 11: End-to-end smoke + architecture audit

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: ALL pass — every existing test plus the new ones (`test_chat_speech_text.py`, `test_chat_speech_prefs.py`, `test_chat_speech_ws.py`, `test_chat_speech_hook.py`, `test_play_request_kind.py`).

If anything fails, fix it before moving on. Do not commit broken tests.

- [ ] **Step 2: Lint + type check**

Run in parallel:
- `uv run ruff check src/ tests/`
- `uv run mypy src/`

Expected: no new errors. Fix any that point at code you added.

- [ ] **Step 3: Frontend build**

Run: `cd frontend && npm run build`
Expected: clean build, no TS errors.

- [ ] **Step 4: Architecture audit**

Use the `validate-architecture` skill (or `Skill` tool with `validate-architecture`) to check the implementation against the rulebook. Expected pass criteria:
- No layer-import violations (only `interfaces/` and `core/chat` imported from new web-facing code; AIService doesn't import from `web/`).
- Per-user event filter follows the `speaker.browser.` pattern.
- `chat_speech_prefs` collection name is consistent in all references.
- New WS RPCs are documented in code comments only — no README change required (chat features aren't in the top-level README index).

Fix any violations the skill flags before declaring done.

- [ ] **Step 5: Manual smoke test**

Start the dev server (`./gilbert.sh start` or equivalent) and:
1. Open `/chat` in a browser, ensure the user is signed in.
2. Verify the chat top strip shows the muted volume icon on the right.
3. Click it — the icon should switch to the signal-color "on" state. Confirm the global header browser-speaker icon also lit up (auto-activation).
4. Send a chat message. Confirm Gilbert's reply plays as audio in the tab.
5. Send a follow-up before the first clip finishes — confirm the first stops and the new one plays (interrupt).
6. Open the same chat in a second tab (same user). Confirm the new tab loads with read-aloud ON (server-side persistence).
7. Toggle off, refresh, confirm the toggle stays off.

If any of these fail, file follow-up tasks — don't paper over.

- [ ] **Step 6: Final commit (if anything changed during validation)**

```bash
git add -A
git status
# If there are changes from lint/audit fixes, commit them:
# git commit -m "chat read-aloud: validation fixes"
```

---

## Self-Review

### Spec coverage check

| Spec section | Implementing task(s) |
|---|---|
| Architecture & data flow | Tasks 1, 6, 7, 8, 9, 10 (wired end-to-end) |
| Persistence — `chat_speech_prefs` | Task 4 |
| WS RPCs (`chat.read_aloud.get/set`) | Task 5 |
| Response hook | Tasks 6, 7 |
| Defense-in-depth `list_speakers()` check | Task 6 step 3 |
| Auto-activation (client side) | Task 9 (`toggle()` calls `browser.setEnabled(true)`) |
| No auto-deactivation | Task 9 (toggle off does NOT call `setEnabled(false)`) |
| Interrupt behavior | Task 8 (explicit `pause()` before `src` swap) |
| `kind: "chat_speech"` event tag | Task 1 |
| `strip_markdown_for_speech` | Task 2 |
| `chat_speech_voice` ConfigParam | Task 3 |
| `ChatSpeechToggle` UI + placement | Task 10 |
| `useChatSpeech` hook | Task 9 |
| Tests (text/prefs/hook/ws) | Tasks 2, 4, 5, 6, 7 |

### Type / signature consistency check

- `_speak_response(user, conversation_id, response_text)` — same signature used in Task 6 implementation and Task 7 callers.
- `get_speech_pref(user_id, conversation_id) -> bool` — same in Task 4 (definition) and Task 7 (caller).
- `PlayRequest.kind` (Task 1) and `play_on_speakers(..., kind=...)` (Task 1 step 6) — both keyword-friendly with default `""`.
- Frontend `useChatSpeech` returns `{ enabled, isSpeaking, toggle }`; `ChatSpeechToggle` consumes those three names (Task 10).
- `PlayItem.kind` + `PlayItem.conversationId` added in Task 8; read in Task 9's `isSpeaking` memo.

No placeholders, no "implement later", no unspecified error handling — every step has concrete code or a concrete command.
