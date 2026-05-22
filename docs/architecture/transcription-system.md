# Transcription System

Aggregator-style service that owns three sibling backend registries
(`BatchTranscriptionBackend`, `StreamingTranscriptionBackend`,
`WakeWordBackend`) and exposes a uniform routing API plus browser-mic
WS plumbing. Sibling-by-design to `SpeakerService` (output) and the
new multi-backend SpeakerService template.

## Why three ABCs

Batch and streaming providers have substantially different shapes
(one-shot bytes vs. open a session and stream events), and wake-word
engines (Porcupine, openWakeWord) are a different class of code from
transcription engines (Whisper, Deepgram). Treating them as three
sibling roles lets one class implement multiple roles (e.g., Deepgram
can subclass both `BatchTranscriptionBackend` and
`StreamingTranscriptionBackend`) while keeping the simple cases simple.

## Backend lifecycle

`TranscriptionService.start()` reads the `transcription` config section,
then calls `_reinit_backends_for_role(role)` for each role. The
reconciler:

- closes any backends that are no longer in `<role>.backends`,
- skips any whose `enabled` is `False`,
- instantiates and `initialize()`s any newly-enabled backends.

Startup errors are captured in `self._startup_failures[role][name]` so
the settings UI can surface them; one failing backend never prevents
others from coming up.

`on_config_changed()` calls the same reconciler — toggling a backend
on or off in settings reinitializes only that one backend, no service
restart.

## Routing

Each public method (`transcribe`, `open_stream`, `open_detector`)
takes an optional `backend=` kwarg. Resolution order:

1. The named backend, if loaded.
2. `self._default_<role>` from config, if loaded.
3. The single loaded backend, if exactly one.
4. `RuntimeError`.

This matches the AI-profile shape and keeps callers blissfully
ignorant of which provider is wired up.

## Browser-mic sessions

`TranscriptionService` is the `ws_handlers` provider for browser
voice control:

| RPC | Direction | Purpose |
|---|---|---|
| `transcription.start_session` | C→S | Opens a stream or wake-word session, returns `session_id` |
| `transcription.send_chunk`    | C→S | Base64-encoded audio chunk; pump task lazily started on first chunk |
| `transcription.close_session` | C→S | Closes the session early |
| `transcription.event`         | S→C | Forwarded transcript / wake events |

`session_id`s are server-minted UUIDs, opaque to clients. One WS
connection may hold N concurrent sessions; the session dictionary is
keyed by `session_id` and each record carries `conn_id` so disconnect
cleanup can find them by connection.

A connection drop fires `conn.add_close_callback(...)`, which
synchronously schedules an async `_close_session(sid)` task. This is
the same cleanup pattern as `BrowserSpeakerBackend`.

## Bundled backend

`src/gilbert/integrations/local_whisper.py` is the bundled
`BatchTranscriptionBackend` using `faster-whisper`. It registers as
`local_whisper` and is the default for the `batch` role. The model
(default `base`) is downloaded by `faster-whisper` to a local cache on
first use; subsequent runs are fast. CPU `int8` is the default
compute mode and works on most laptops.

## Deliberate v1 omissions

These are listed in the spec for completeness — they are tracked, not
forgotten:

- **No wake-word orchestration helper** (no `listen_with_wake_word(...)`):
  primitives only. Will land once a real consumer exists so the
  helper's shape is informed by use.
- **No SPA voice panel:** WS RPCs work server-side; UI is a follow-up.
- **No transcript persistence:** `transcribe` writes a transient
  output file under `output_ttl_seconds` cleanup; no entity collection.
  If you want transcripts searchable, ingest them via the knowledge
  service instead.
- **No streaming `transcribe`:** the batch path loads the whole file.
  Fine for chat attachments and voicemails; revisit if a caller hits
  the limit.
- **No live translation, no per-user session caps, no usage dashboards.**
