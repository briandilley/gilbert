# Chat Read-Aloud — Per-Chat Mute/Unmute via Browser Speaker

**Status:** design approved, awaiting implementation plan
**Date:** 2026-05-17

## Summary

Add a per-chat mute/unmute button in the chat header that, when unmuted, makes Gilbert read his responses aloud through the user's browser-speaker tab. Server-side TTS is triggered on chat-turn completion and routed via the existing browser-speaker plumbing.

State is persisted per `(user_id, conversation_id)` on the server. Toggling read-aloud on auto-activates the user's browser speaker. A new response interrupts an in-flight clip.

## Architecture & data flow

```
SPA chat header  ──(WS RPC: chat.read_aloud.set)──▶  AIService
                                                      │
ChatPage local state ◀──(WS event: chat.read_aloud.changed)── stored as
                                                      │   chat_speech_prefs entity
                                                      ▼
User submits message ─▶ AIService runs turn ─▶ response_text
                                                      │
                              read-aloud on?  yes ───▶ TTSService.synthesize(plain_text)
                                                      │
                                                      ▼
                              SpeakerService.play_on_speakers(
                                  ["browser:<user_id>"],
                                  PlayRequest(announce=False, conversation_id=conv_id))
                                                      │
                                                      ▼  (existing browser-speaker plumbing)
                              speaker.browser.play  ─▶  user's tab only ─▶ <audio> element
```

The server already owns TTS, per-user browser routing, and a singleton browser-tab audio element. The decision point (per-user-per-conv preference) sits at the only place that knows both the user and the conv — the AIService turn-completion path. The SPA just toggles a flag and lets the existing browser-speaker machinery handle delivery and interrupt.

## Persistence — `chat_speech_prefs` entity collection

New entity collection (no migration to `conversations`):

```python
# interfaces/chat_speech.py
@dataclass
class ChatSpeechPref:
    user_id: str
    conversation_id: str
    enabled: bool
    updated_at: str
```

`_id` convention: `f"{user_id}:{conversation_id}"`.

**Service ownership:** AIService owns the collection (since it owns conversations and the turn-completion hook). Two helpers:

- `get_speech_pref(user_id, conv_id) -> bool` — defaults to `False` for missing entries.
- `set_speech_pref(user_id, conv_id, enabled) -> None` — upserts and broadcasts a `chat.read_aloud.changed` event with payload `{user_id, conversation_id, enabled}`. The event is delivered only to the matching `user_id` (added to the per-user filter chain in `web/ws_protocol.py`, same shape as `speaker.browser.*` filtering).

**Why a side collection** rather than a field on the conversation doc:

- Conv docs are read constantly by sidebar/history; bolting a per-user map onto every conv balloons reads.
- Shared rooms have N members with N preferences — a side collection naturally keys on `(user_id, conv_id)`.
- No conv-schema migration.

**RBAC:** `set_speech_pref` requires `check_conversation_access(conv, user, require_member=True)` — the same gate the chat history endpoint uses. You can only toggle your own preference for a conv you have access to.

## Server-side wiring

### WS RPCs (AIService)

- `chat.read_aloud.get {conversation_id}` → `{enabled: bool}` — used on chat open.
- `chat.read_aloud.set {conversation_id, enabled}` → `{enabled: bool}` — persists and broadcasts a per-user `chat.read_aloud.changed` event so other tabs of the same user stay in sync.

### Response hook

In the existing turn-completion path (the same point that returns `response_text` to the WS caller — `_ws_chat_form_submit` and analogous chat handlers):

```python
# after the turn completes successfully, before returning the WS frame
if response_text.strip() and await self.get_speech_pref(user.user_id, conv_id):
    asyncio.create_task(self._speak_response(user, conv_id, response_text))
```

Fire-and-forget so it never delays the chat reply. `_speak_response`:

1. `plain = strip_markdown_for_speech(response_text)`.
2. If `plain` is empty after stripping → return (don't synthesize whitespace).
3. `tts = self._resolver.get_capability("text_to_speech")` — if missing or disabled, log and return.
4. **Defense in depth:** check `speaker_svc.list_speakers()` for a `browser:<user_id>` entry; skip if absent (avoids paying TTS cost for undeliverable audio).
5. `audio = await tts.synthesize(SynthesisRequest(text=plain, ...))` using a new `chat_speech_voice` config param on AIService (`ConfigParam(default="")`; empty means "use the TTS service's default voice"). No `chat_speech_voice` UI in v1 — it's a config knob, not a per-chat or per-user choice.
6. Write to the output dir (same path the existing announce flow uses) → URL.
7. `speaker_svc.play_on_speakers([f"browser:{user.user_id}"], PlayRequest(uri=url, announce=False, conversation_id=conv_id, title="Gilbert"))`.

`announce=False` is deliberate — the announce path is the duck-and-restore code path for Sonos; the browser tab just needs plain playback.

### Failure handling

Any exception is logged at warning level and swallowed. A TTS failure must never break a chat reply.

### Shared rooms

In v1, only the user who *sent* the message gets audio — the hook runs on their AIService call. Other members in the room with read-aloud on for the same conv won't hear it. Triggering per-member chat-speech in shared rooms requires fanning out off the `chat.message.created` event and is deferred.

## Auto-activation of browser speaker

### Client side

When the user flips read-aloud on for a chat:

1. If `useBrowserSpeaker().activated === false`, call `useBrowserSpeaker().activate()` first (which sets the localStorage flag and sends `browser_speaker.activate` over the WS).
2. Then send `chat.read_aloud.set {enabled: true}`.

Order matters: activate first so the user is a registered browser speaker by the time the first `play_on_speakers` fires. Otherwise the first response would publish to a user with no listener and silently drop.

### Server side

The defense-in-depth `list_speakers()` check in `_speak_response` step 4 catches edge cases — e.g., user toggled read-aloud on from a tab and then closed it. Skip TTS entirely rather than synthesizing audio that will never play.

### No auto-deactivation

Toggling chat-speech *off* does not deactivate the browser speaker. The user may have it on for music or other reasons. Activation is a strict superset; chat-speech is one of many possible producers.

## Interrupt behavior

When a new chat response arrives mid-clip, the previous clip stops and the new one plays immediately.

### Client implementation

`BrowserSpeakerProvider` in `useBrowserSpeaker.tsx` already manages a singleton `<audio>` element. The current event-handler appears to overwrite `audio.src` on each new `speaker.browser.play` event, which the browser already treats as interrupt (the previous load is aborted). Verify during implementation; if the current implementation queues or coexists with prior loads, fix to:

```ts
if (!audioRef.current.paused) audioRef.current.pause();
audioRef.current.src = nextUrl;
audioRef.current.play();
```

### Server side

No work — the server just fires `play_on_speakers` again. The client audio element handles the interrupt.

### Tagging chat-speech clips

Add a `kind: "chat_speech"` field to the `speaker.browser.play` event payload (currently carries `user_id`, `conversation_id`, `url`, `title`, `volume`, `announce`, `position_seconds`). This lets the SPA:

- Show a subtle "speaking…" indicator in the chat header while a chat-speech clip is playing. The SPA tracks "playing" via the audio element's `play` / `pause` / `ended` events on the currently-loaded chat-speech clip, scoped to the current `conversationId`.
- Exclude chat-speech clips from the browser-speaker history popover (it would fill up immediately and isn't replay-worthy).

## Speech-friendly text rendering

`strip_markdown_for_speech(text: str) -> str` — new helper in `core/chat.py`. Transformations:

| Markdown | Speech form |
|---|---|
| ```` ```fenced code``` ```` | dropped |
| `` `inline code` `` | plain inner text |
| `**bold**`, `*italic*`, `_underline_` | plain text |
| `# Heading` | `Heading.` |
| `- item` (list) | `item.` |
| `[text](url)` | `text` (URL dropped) |
| `![alt](url)` (image) | dropped |
| HTML tags (belt-and-suspenders) | stripped |
| Runs of blank lines | collapsed to `". "` |

Regex-based; no markdown-library dependency. Headings and list items get trailing periods so TTS pauses between them. If the response was only code, the spoken text becomes empty and `_speak_response` short-circuits at step 2.

**Location:** `core/chat.py` next to `mentions_gilbert` — shared chat business logic that imports only from `interfaces/`.

## Frontend UI

### `ChatSpeechToggle.tsx`

New component, added to `ChatPage.tsx`'s compact top strip on the right side. Same visual treatment as the global `BrowserSpeakerControl`: icon-only, no border, hover background, signal-color when active.

- **Off:** `VolumeXIcon`, `text-foreground/60`.
- **On:** `Volume2Icon`, `text-(--signal)`. While a chat-speech clip is currently playing (detected via the `kind: "chat_speech"` event tag from the interrupt section), animate the icon (pulse, or swap to a 3-bar speaker animation — exact motion left to implementation).

### `useChatSpeech(conversationId)` hook

Mirrors the shape of `useBrowserSpeaker`:

- On mount, RPC `chat.read_aloud.get` to hydrate.
- Exposes `{ enabled, toggle() }`.
- Subscribes to `chat.read_aloud.changed` events (per-user filtered) so other tabs of the same user stay in sync.
- `toggle()` performs the auto-activation dance: activate browser speaker if needed, then `chat.read_aloud.set`.

**No client-side persistence** — server is the source of truth. The hook caches the value for the current `conversationId` and re-hydrates when it changes.

## Tests

### Unit tests (`tests/unit/`)

- **`test_chat_speech_text.py`** — table-driven cases for `strip_markdown_for_speech`:
  - fenced code dropped, inline code kept as plain text, headings periodized, lists periodized, links keep text drop URL, images dropped, HTML tags stripped, whitespace collapsed, empty-after-strip case returns empty.

- **`test_chat_speech_prefs.py`**:
  - `get_speech_pref` defaults to `False` for missing entries.
  - `set_speech_pref` persists; round-trip via `get_speech_pref`.
  - `set_speech_pref` enforces conversation access (denied for non-member).

- **`test_chat_speech_hook.py`** — mock TTS + SpeakerService + entity store:
  - Pref off: no synthesis call.
  - Pref on: synthesis called, `play_on_speakers` called with `["browser:<user_id>"]`.
  - TTS exception is swallowed and logged.
  - User without an active browser speaker: synthesis skipped (defense-in-depth from §4).

- **`test_chat_speech_ws.py`** — WS frames `chat.read_aloud.get`/`set` happy paths plus access-denied and missing-conv error cases.

### Integration

None required for v1. The existing browser-speaker integration tests cover the `play_on_speakers` → `speaker.browser.play` → WS filter path that this feature reuses.

### Frontend

No new tests — the project doesn't have a frontend test harness today, and the surface is small (one icon + one hook).

## Out of scope (v1)

- Per-member chat-speech in shared rooms (only the user who sent the message gets audio).
- Voice selection per chat (uses the AIService-level `chat_speech_voice` config; no UI).
- Per-turn replay button (master toggle only).
- Reading aloud non-Gilbert messages (other room members' messages).
- Speed/pitch controls.

## Known limitations

- Read-aloud silently no-ops if TTS is disabled (no UI to warn the user). Reasonable: same behavior the rest of the announce path has.
- If the user opens the same chat in two tabs both with browser-speaker active, both tabs will hear the audio (this is how browser-speaker per-user routing already works — every active connection for that user gets the event).
