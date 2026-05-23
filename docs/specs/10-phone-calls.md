# Feature 10: Outbound Phone Calls — Gilbert as a Phone Assistant

> **Status:** plan / RFC. No code yet — iterate on this doc first.

## Target scenario

> "Hey Gilbert, Flatirons Audi at (303) 555-0100. Tell them you're my assistant
> and you need to setup service for my 2026 Audi RS6, purchased in December of
> 2025. It has a recall for a backup camera, and I'm getting an error message
> on the screen that says 'Faulty right turn signal.' I'd like to bring it in
> sometime next week, in the morning. They can call me back at 704-641-1948 if
> they have any questions, but I'd like you to make the appointment. I need a
> loaner if possible!"

Gilbert places the call, navigates whatever IVR or receptionist greets him,
explains who he is and why he's calling, books a service appointment with the
constraints above, and reports back. The user can watch the live transcript
and intervene by text or voice if the call gets stuck.

## Decisions already made

| Decision | Choice | Why |
|---|---|---|
| **Telephony provider** | **Telnyx** | ~4× cheaper than Twilio, comparable API shape, Media Streams over WebSocket. |
| **MVP scope** | **Full two-way conversation** | Voicemail-only would be a fork; the conversation loop is the hard part anyway. |
| **Supervision** | **Live transcript + intervene** (text + voice) | The receptionist-confusion long-tail is too risky for fire-and-forget. |
| **STT** | reuse `transcription` service | Deepgram or ElevenLabs Scribe streaming — already wired into a `TranscriptionBackend` ABC. |
| **TTS** | reuse `tts` service | ElevenLabs Flash 2.5 streaming — already configured on meridian. |
| **LLM** | reuse `ai_chat` service | Same Claude/whoever the user has set. Tooling already exists. |

## Architecture sketch

```
src/gilbert/interfaces/
    telephony.py          ← new ABC + dataclasses
src/gilbert/core/services/
    phone_call.py         ← new PhoneCallService (Service, ToolProvider, WsHandlerProvider)
std-plugins/
    telnyx/               ← new std-plugin
        plugin.py
        telnyx_telephony.py  ← TelnyxTelephony(TelephonyBackend)
frontend/src/
    components/calls/
        PhoneCallPanel.tsx    ← live transcript + intervene UI
    hooks/
        usePhoneCall.ts       ← subscribe + dispatch
```

### `TelephonyBackend` ABC

```python
class CallStatus(StrEnum):
    INITIATED = "initiated"
    RINGING = "ringing"
    CONNECTED = "connected"
    HUNG_UP = "hung_up"
    FAILED = "failed"
    VOICEMAIL = "voicemail"      # detected by call brain, not the backend

@dataclass
class CallSession:
    call_id: str
    audio_in:  AsyncIterator[bytes]    # mulaw 8kHz from the other party
    audio_out: AsyncSink[bytes]        # mulaw 8kHz to the other party
    events:    AsyncIterator[CallEvent]  # status changes, DTMF, hangup
    async def hang_up(self) -> None: ...

class TelephonyBackend(ABC):
    backend_name: ClassVar[str]
    @abstractmethod
    async def initialize(self, config: dict) -> None: ...
    @abstractmethod
    async def place_call(
        self, *, to_number: str, from_number: str, call_id: str,
    ) -> CallSession: ...
    @abstractmethod
    async def close(self) -> None: ...
```

Mirrors the existing `SpeakerBackend` / `TTSBackend` pattern exactly:
`__init_subclass__` auto-registry, `backend_config_params()`, swap concretes
without touching `PhoneCallService`.

### `PhoneCallService`

- **Capabilities provided:** `phone_calls`, `ai_tools`, `ws_handlers`
- **Capabilities required:** `text_to_speech`, `speech_to_text`, `ai_chat`
- **Entity storage:** `phone_calls` collection — one row per call with
  status, brief, transcript, outcome summary, recording URL, costs
- **AI tool:** `make_phone_call(to_number, brief, callback_number?)` — the
  user-facing slash from inside Gilbert chat
- **WS handlers:**
  - `phone.call.list` — list recent calls
  - `phone.call.get` — full transcript + status
  - `phone.call.intervene_text` — user types a directive mid-call
  - `phone.call.intervene_voice` — user mic stream → straight into `audio_out`
  - `phone.call.hang_up` — bail
- **Events emitted:**
  - `phone.call.started` / `.ended` / `.failed`
  - `phone.call.transcript_delta` — every confirmed STT chunk
  - `phone.call.status_changed` — ringing → connected → hung up
  - `phone.call.summary` — final structured outcome

### The call brain (inside `PhoneCallService._run_call`)

```python
async def _run_call(session, brief):
    history = [{"role": "system", "content": build_call_system_prompt(brief)}]
    transcript = TranscriptBuffer()
    speaking = SpeakingState()  # are we currently outputting TTS?

    async def listen_loop():
        async for partial in transcription.open_stream(session.audio_in):
            if partial.kind == "start_of_speech" and speaking.active:
                # Barge-in: cancel our outbound TTS.
                speaking.cancel()
            if partial.kind == "final":
                transcript.append("them", partial.text)
                history.append({"role": "user", "content": partial.text})
                await think_and_speak()

    async def think_and_speak():
        # Stream from the LLM; pipe deltas through TTS, push bytes into audio_out.
        async for delta in ai.chat_stream(history, tools=call_tools()):
            if delta.text:
                speaking.start()
                async for chunk in tts.stream(delta.text):
                    if speaking.cancelled: break
                    await session.audio_out.write(chunk)
            if delta.tool_call:
                await handle_tool(delta.tool_call)  # hang_up, escalate, etc.

    await asyncio.gather(listen_loop(), wait_for_hangup(session))
```

**Tools the LLM has during a call:**

- `hang_up(reason)` — explicit goodbye + tear down
- `confirm_and_end(summary)` — read back the summary, wait for "yes", hang up
- `escalate_to_user(reason)` — notify the user, mute Gilbert, pass to live
- `note(key, value)` — write structured facts to the call's outcome
- `send_dtmf(digits)` — for "press 2 for service"

## Data model (`phone_calls` collection)

```json
{
  "_id": "call_2026_05_23_abc123",
  "user_id": "usr_jeremy",
  "to_number": "+13035550100",
  "from_number": "+17046411948",
  "callback_number": "+17046411948",
  "brief": "Setup service for my 2026 Audi RS6, recall + faulty turn signal…",
  "status": "completed",
  "started_at": "2026-05-23T15:30:12Z",
  "ended_at":   "2026-05-23T15:38:47Z",
  "duration_seconds": 515,
  "cost_usd": 0.0177,
  "transcript": [
    {"who": "them", "text": "Flatirons Audi service, Tracy speaking.", "ts": 4.1},
    {"who": "us",   "text": "Hi Tracy, I'm Gilbert, calling on behalf …", "ts": 5.3},
    …
  ],
  "outcome": {
    "appointment_booked": true,
    "appointment_datetime": "2026-05-28T09:30:00-06:00",
    "loaner_confirmed": true,
    "service_advisor": "Tracy Gomez",
    "ticket_number": "RS-8821",
    "notes": "Recall + turn-signal diagnosis confirmed in the work order."
  },
  "recording_url": "telnyx://…",
  "interventions": [
    {"who": "user", "ts": 142.6, "text": "Ask if there's a loaner SUV available."}
  ]
}
```

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Latency feels robotic** — each turn is audio→STT→LLM→TTS→audio, target <1.5s | Streaming everywhere. ElevenLabs Flash 2.5 → first audio in ~200ms. Deepgram partials → start LLM on partial, refine on final. Pre-warm TTS connection on call connect. |
| **Voice quality on G.711 8kHz mulaw** | ElevenLabs has an `mulaw_8000` output format. Test before relying on it. |
| **Barge-in / talking over them** | Listen for start-of-speech on the inbound stream while we're outputting; cancel `audio_out` writes immediately. Telnyx supports `clear` to drop our buffered audio. |
| **Voicemail detection** | Heuristics: silence > 6s after pickup, classic "leave a message after the beep" phrasing, beep tone detection. On detect, switch to a one-shot prepared message + hang up. |
| **IVR menus** | LLM has `send_dtmf(digits)` tool. Prompt: "If you hear a menu, navigate it. If unclear, press 0 or stay silent for operator." |
| **"Are you a robot?"** | Disclose: "Yes, I'm an automated assistant calling on behalf of Jeremy. I can take notes and confirm appointment details — would you prefer Jeremy call back directly?" |
| **Hallucinated commitments** | Strong system prompt: "Never confirm a time the receptionist hasn't offered. Read back times verbatim before agreeing." Confirmation step at end-of-call. |
| **Legal disclosure (CO is one-party recording, but federal AI-call rules tightened in 2024)** | Disclose AI status in the opening greeting. Optional: append "this call may be recorded" if the user opts in. Both controlled by a per-user policy in settings. |
| **Misunderstood appointment** | End-of-call `confirm_and_end` tool reads the structured outcome back. User notification includes the structured outcome with a "looks wrong" button that opens the chat for follow-up. |
| **Receptionist insists on speaking to the customer** | `escalate_to_user(reason)` tool — Gilbert mutes himself, fires a push notification, the user joins via WebRTC from the SPA. (Phase 4.) |
| **Cost runaway** | Per-call timeout (default 15 min hard cap), daily spend cap in settings, push notification at $X spent. |

## Phase plan

| Phase | Scope | Definition of done | Est. |
|---|---|---|---|
| **1. Audio plumbing** | `TelephonyBackend` ABC + `telnyx` std-plugin + skeleton `PhoneCallService`. Outbound call to a hardcoded number, plays a static WAV, hangs up after 10s. SPA shows status. | Live call placed from `/settings` test button. | 3-5d |
| **2. Live STT/TTS loop** | Wire transcription + TTS into the call. Hardcoded "echo bot" brain: STT in → TTS reads it back. Verify latency budget (<1.5s end-to-end). | Call yourself, you say "hello," Gilbert says "you said hello" within 1.5s. | 3-5d |
| **3. AI brain (the MVP)** | LLM in the loop with the user's brief. Turn-taking via barge-in. Voicemail detection. Persist transcript. `make_phone_call` AI tool. SPA panel with live transcript + intervene-by-text. Callback routing for the shared number. Concurrency cap (1/user). | The Audi scenario above works end-to-end on a real number. | 1-2w |
| **4. Real-world hardening** | IVR (DTMF), hold-music tolerance, "are you a bot" templates, end-of-call confirmation tool, recording playback, cost caps. Escalation = hang-up + push-notification with callback prompt (no WebRTC in v1). | Pass an internal "100 calls" reliability bar against the local-business test set. | 1-2w |

After phase 3 the Audi scenario works. Phase 4 makes it reliable enough to
trust without supervision.

## Resolved policy (the six open questions)

| # | Question | Decision | Implication |
|---|---|---|---|
| 1 | From-number | **Single shared "Gilbert" number** | One Telnyx line for all users (~$1/mo). Caller-ID always says "Gilbert" / the shared number. Callbacks land at the shared number and route via the *to-from-and-time* tuple back to the right call record. |
| 2 | Recording policy | **Always record** | Telnyx records every call automatically. The opening greeting must include the AI-disclosure + recording notice — non-optional. |
| 3 | Identity | **Gilbert** | The assistant identifies itself as Gilbert; the user is referenced as "on behalf of \<display_name\>." No per-user "calling-as" override in MVP. |
| 4 | Voice intervene mid-call | **Punt** | No WebRTC for v1. Intervene-by-text only. Escalation = Gilbert apologizes, hangs up, fires a push notification with the transcript + a "ready to be called back at \<number\>?" follow-up. |
| 5 | Concurrency cap | **1 active call per user** | The `make_phone_call` tool refuses if there's already an active call for the caller. Queued calls are out of scope. |
| 6 | Calendar dependency | **Degrade gracefully** | If `calendar` capability is registered, the brain uses `find_free_time` to propose specific morning slots; otherwise it says "I have flexibility next week — what mornings work on your end?" and pipes the receptionist's offers straight to the user for confirmation. |

### Knock-on design changes from those decisions

- **Callback routing.** Because there's one shared number, callbacks need a
  way to find their original call. Approach: on inbound call to the shared
  number, look up the most recent active or recently-ended call whose
  `to_number == caller_id`. If found, the inbound caller is routed into
  Gilbert with the original brief + transcript as context. If not found,
  Gilbert greets them as a stranger and asks who they're trying to reach. This
  isn't free — it's a small slice of inbound-call work pulled into MVP, but
  the alternative (callbacks black-hole at the shared line) is worse UX.
- **Disclosure script.** First sentence of every call is now policy:
  > "Hi, this is Gilbert, an automated assistant calling on behalf of
  > \<display_name\>. This call is being recorded for quality."
  Lives in `phone_call.opening_disclosure_prompt` as a `ConfigParam(ai_prompt=True)`
  so the operator can tune the wording without code changes.
- **Escalation flow.** `escalate_to_user(reason)` tool wraps the call in this
  exact sequence: (1) Gilbert says "Let me have \<display_name\> call you
  back to take care of this — what's the best number?" (2) captures the
  number and a short note, (3) hangs up, (4) fires a push notification with
  the captured info + a one-tap "call them back" action. Simpler than WebRTC
  and probably better UX 80% of the time.
- **Concurrency enforcement.** Trivially per-user: `PhoneCallService` keeps
  a `dict[user_id, CallSession]` of active sessions; `make_phone_call` checks
  it before placing. Existing call exposed via `phone.call.list` (active
  filter) so the SPA can offer "cancel the active call to make a new one."

## Things I deliberately punted

- **Inbound calls — except minimal callback routing.** General-purpose inbound
  (who can call, what's the greeting, when does Gilbert handle alone vs route
  to a user) is its own spec. The narrow case where someone calls our shared
  number BACK after Gilbert called them, however, IS in MVP — see "callback
  routing" under resolved policy.
- **Voice intervene mid-call (WebRTC).** Punted per question 4. Intervene-by-
  text covers the common case; the rare "I really need to be on the line"
  case becomes "hang up + push notification with the callback number."
- **Per-user calling-as identity.** Punted per question 3. Gilbert is Gilbert.
- **Queued calls.** Punted per question 5. One active call per user.
- **Recording transcription post-hoc.** Telnyx provides a recording URL; we
  can run our own STT over it for higher-fidelity transcript than the live
  stream gave us. Nice-to-have for the call detail view.
- **SMS follow-ups.** "Send a confirmation text to Tracy" — possible via
  Telnyx Messaging API but out of MVP scope.
- **Wake-word "Hey Gilbert" outside the SPA chat.** Phone calls assume the
  user is in chat. The wake-word case routes through the same AI tool, so
  it'll work the day wake-word ships, but we're not designing for it here.

---

Spec is decision-complete. Next step: spin up a `feature/phone-calls` branch
and start phase 1 — `TelephonyBackend` ABC, `telnyx` plugin skeleton, an
outbound call from `/settings` that plays a hardcoded WAV and hangs up. ETA
3-5 days of focused work.
