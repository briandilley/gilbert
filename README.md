# Gilbert

An AI-powered assistant for home and business automation. Gilbert combines a modular, interface-driven architecture with an agentic AI core — giving it the ability to control speakers, greet people at the door, manage email, spin up a radio DJ, expose its tools over MCP, and much more, all orchestrated through natural conversation or automated event-driven workflows.

Everything in Gilbert is an abstraction. Swap your AI provider, your speaker system, your presence detector, or your storage backend without touching a single line of business logic. The core ships with only vendor-free backends (local auth, local filesystem documents, local + browser speaker playback, local Whisper speech-to-text, MCP transports); every third-party integration — Anthropic, Sonos, Google, UniFi, ElevenLabs, Tavily, Slack, ngrok, Tesseract — is a **plugin**. Plugins live in a separate [gilbert-plugins](https://github.com/briandilley/gilbert-plugins) repo that's included as a git submodule at `std-plugins/`, and new plugins can be added at runtime from any GitHub URL.

Gilbert is a **multi-user system from the ground up** — every piece of state (mailboxes, chat history, documents, MCP servers, scheduled jobs) is owned by a specific user, shared via roles and per-collection ACLs, and gated by a role-based access control layer that consistently applies across the web UI, chat, tools, events, and the MCP endpoint.

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package and project manager)
- Git (with submodule support — any recent version)
- Node.js + npm — `gilbert.sh start` rebuilds the frontend SPA on every launch (the compiled bundle under `src/gilbert/web/spa/` is gitignored, so a fresh clone has to build it).

Some plugins have additional OS-level prerequisites — e.g. the `tesseract` plugin needs the Tesseract binary (`apt install tesseract-ocr`, `brew install tesseract`, or `pacman -S tesseract tesseract-data-eng` on Arch — note that Arch ships language data as a separate package). Check [`std-plugins/README.md`](std-plugins/README.md) for per-plugin requirements.

### Clone and Install

```bash
git clone https://github.com/briandilley/gilbert.git
cd gilbert

# Either let gilbert.sh init the submodule for you...
./gilbert.sh start

# ...or do it manually:
git submodule update --init --recursive
uv sync
```

`uv sync` resolves the entire workspace — Gilbert core plus every plugin's third-party deps — into a single venv. If you add or update a plugin later, re-running `uv sync` picks up the changes.

### Configure

Gilbert ships with sensible bootstrap defaults in `gilbert.yaml`. Only a small handful of settings live in YAML — storage, logging, web server binding, and the plugin directory list — because those are needed before entity storage is available. **Everything else is configured at runtime through the Settings UI** under the **System → Settings** menu at `http://localhost:8000/settings`.

On first run, non-bootstrap sections from `gilbert.yaml` (AI config, plugin config, etc.) are seeded into the entity store. After that, the Settings UI is the source of truth — changes there persist to `.gilbert/gilbert.db` and take effect immediately (or on restart for params marked `restart_required`).

If you need to override bootstrap values for this specific installation, create a local override file:

```bash
mkdir -p .gilbert
cat > .gilbert/config.yaml <<'EOF'
# Only include values you're changing. Deep-merged on top of gilbert.yaml.
auth:
  root_password: "pick-a-strong-password"
web:
  port: 9000
EOF
```

The `.gilbert/` directory is gitignored — your API keys, database, and logs stay local. Runtime config (AI backend selection, TTS API keys, plugin settings, etc.) is managed through the Settings UI, not this file.

**Set a root password before the first boot.** On first run Gilbert seeds non-bootstrap YAML values into entity storage and from then on the database is the source of truth — so editing `.gilbert/config.yaml` after the first start has no effect on already-seeded keys. If you boot without `auth.root_password` set, the bootstrapped `root` user is created with no usable password and nobody can log in (local visitors get the `everyone` role, which can't reach Settings). To recover, stop Gilbert (`./gilbert.sh stop`), delete `.gilbert/gilbert.db*`, add `auth.root_password` to `.gilbert/config.yaml`, and start again. The admin username is `root`.

At minimum, before Gilbert is useful, open the Settings UI and configure:

- **AI** → select the `anthropic` backend and enter your API key (`sk-ant-…`).
- **Whatever plugins you care about** — e.g. Sonos speakers (discovery is automatic), Google personal-account or Workspace backends, UniFi presence (host + credentials), and so on. Each plugin's settings page has a **Test connection** button to verify credentials before you commit to them.

### Run

```bash
# Start Gilbert (auto-inits the std-plugins submodule if empty, then uv sync, then launches)
./gilbert.sh start

# Dev mode (same as start, with verbose logging suitable for iteration)
./gilbert.sh dev

# Stop Gilbert (sends SIGTERM to the running PID)
./gilbert.sh stop
```

`gilbert.sh start` runs Gilbert under a supervisor loop that distinguishes normal stops from "please restart me" exits — when a runtime-installed plugin needs `uv sync` to pick up new Python deps, it sets an internal flag, Gilbert exits with code `75` (`EX_TEMPFAIL`), and the supervisor loop re-syncs and relaunches automatically. Ctrl+C and `./gilbert.sh stop` propagate cleanly and do **not** trigger a restart.

On first run, Gilbert creates the `.gilbert/` directory and initializes the SQLite database, log files, and default AI profiles. The web UI is available at `http://localhost:8000` — log in as `root` with the password you set under `auth.root_password` in `.gilbert/config.yaml` (see [Configure](#configure)), then head to **Security → Users** to add more accounts.

Gilbert also serves HTTPS on `https://localhost:8443` using a self-signed certificate auto-generated on first boot under `.gilbert/credentials/tls.{crt,key}`. The HTTPS listener is what unlocks browser features that require a secure context — notably `getUserMedia` for the voice agent and any other mic/camera-driven flow. Open `/setup-https` for the cert download and per-OS install steps so phones and other LAN browsers can trust it. Disable by setting `web.tls.enabled: false` in `.gilbert/config.yaml` if you terminate TLS elsewhere.

## Multi-User & Access Control

Gilbert is designed for households and small teams, not a single desktop user. Every request — whether from the web UI, a Slack DM, a chat with the AI, or an external MCP client — is attributed to a specific user and passed through a consistent authorization layer.

- **Users.** Local accounts (`LocalAuth` in core) or external identity providers (`GoogleAuthBackend` in the `google` plugin). Users are managed under **Security → Users** in the web UI; an admin can create accounts, assign roles, and configure per-user mailbox access. External directory backends like Google Workspace can auto-sync user lists.
- **Roles.** The built-in role hierarchy is `admin > user > everyone`. Every tool, RPC method, event, and entity collection declares a `required_role` that the caller's effective role level has to meet or exceed. Custom roles can be added on top. Managed under **Security → Roles**.
- **Per-capability ACLs.** RBAC isn't a single boolean — it's layered:
  - **Security → Tools** — per-tool role requirements (e.g. the `delete_document` tool requires `admin`, the `play_music` tool accepts `user`).
  - **Security → AI Profiles** — named bundles of tool allowlist + backend + model that any AI call resolves through. Built-in profiles (`light` / `standard` / `advanced`) are tier-shaped; services that drive the AI (chat, greeting, scheduler, Slack, MCP sampling, etc.) each declare an `ai_profile` config so admins can route different use cases to different tiers/models. Profiles control *which* tools are available; RBAC controls *who* can invoke them. Both always apply. Pure text-generation callers (greetings, roasts) force zero tools at the call site (`complete_one_shot(tools_override=[])`) so their profile choice only selects backend/model — they can't accidentally invoke tools like `announce`.
  - **Security → Collections** — per-entity-collection read/write ACLs for the generic storage layer, so e.g. `inbox.mailboxes` can be read by `user` but only written by the mailbox owner.
  - **Security → Events** — per-event-type visibility so sensitive event types (e.g. presence updates, doorbell events) don't leak to clients who shouldn't see them.
  - **Security → RPC** — per-WebSocket-method permissions for direct RPC frames outside the tool pipeline.
- **Ownership.** Mailboxes, knowledge sources, MCP servers, and scheduled jobs are *owned* by a user and can be shared with individual users or roles. Shared items respect the owner's access chain — an admin sharing their mailbox with a `user` grants read/send but not ownership transfer.
- **MCP multi-user support.**
  - **Client side (Gilbert → external servers):** each MCP server record has a `scope` (`private` / `shared` / `public`) and an optional `allowed_users` list. The rule is "if you can see it, you can use it" — discovery and invocation are gated by the same visibility check.
  - **Server side (external clients → Gilbert):** Gilbert exposes its own tools as an MCP server at `/api/mcp`. Admins register external clients under **MCP → Clients**, pick an owner user and an AI profile, and hand the client a one-time bearer token. Every tool call from that client runs under the owner's `UserContext` with the profile's tool allowlist applied, so external agents can't see or call anything their owner couldn't.
  - **Browser-bridged local servers (user → their own machine):** any user can point Gilbert at MCP servers running on their own laptop (or their LAN) under **MCP → Local** without opening firewall holes. The browser tab acts as a transport proxy — Gilbert sends MCP JSON-RPC calls over the authenticated WebSocket, the tab POSTs them to a URL the user configured locally, and the results flow back the same way. These entries are session-ephemeral, strictly private to the owning user (invisible to admins), and disappear the moment the tab closes. No server-side config, no tunnel, no extra process to run.
- **Consistent across surfaces.** The same authorization layer filters the web UI nav (items you can't access simply don't appear), the tool set the AI sees in chat, the commands available in slash-command autocomplete, the events streamed over WebSocket, and the tool list returned to an MCP client's `tools/list` call.

## What Can It Do?

Out of the box — once the `std-plugins` submodule is initialized — Gilbert provides:

- **AI chat** with tool use — ask Gilbert to play music, check who's home, search your documents, compose an email, or push content to a wall-mounted display. Claude is the default AI via the `anthropic` plugin; swap it for any other backend that implements `AIBackend`.
- **Presence detection** — know who's home (and where) via WiFi clients, cameras with facial recognition, and badge readers. The `unifi` plugin aggregates UniFi Network, UniFi Protect, and UniFi Access signals into a single presence stream.
- **Doorbell monitoring** — detect ring events from UniFi Protect cameras and announce visitors over your speakers with a custom TTS voice.
- **Object-detection cameras (Frigate)** — the `frigate` plugin subscribes to Frigate's MQTT event stream for live object-detection events ("person at the side gate", "package on the porch", "car in the driveway") and exposes AI tools (`list_cameras`, `latest_clips`, `get_snapshot`, `who_was_seen`, `count_detections`). Snapshots and clips proxy through Gilbert with `Range` support so a `<video>` tag works over a tunnel without exposing your Frigate URL or auth token. Per-camera role overrides admin-gate sensitive cameras (bedrooms, vault) without leaking events to non-admin sockets, and the greeting service can announce package deliveries across adjacent cameras as a single event via configurable zone groups + per-label dedup keys.
- **Music and speaker control** — the `sonos` plugin discovers Sonos speakers on the LAN, handles playback/volume/grouping, and uses Spotify's Web API for browse/search. The Music service exposes search, queue, station ("play more like this"), and loop/repeat tools — capabilities are gated per-backend so swappable backends only surface what they actually support.
- **Video library + casting** — the `media_library` service aggregates Plex and Jellyfin via a multi-backend ABC (`MediaLibraryBackend`). The `plex` and `jellyfin` plugins each register a backend; the service fans library queries out across both, merges results, and dispatches playback to whichever server owns the target client. AI tools (`/media search`, `/media recent`, `/media on-deck`, `/media now`, `/media play <title> on <client>`, `/media pause` / `/media resume` / `/media stop` / `/media seek`, `/media recommend`) cover the common cases. Per-user identity mapping persists Gilbert↔Plex-Home and Gilbert↔Jellyfin-user pairs so resume / on-deck reflect the calling user's library, never an admin-token leak. `play_on` for a SHOW resolves to the user's next-unwatched / on-deck episode before dispatch — no silently playing the pilot. Visual UIBlock disambiguation when a search matches multiple high-confidence items, state-aware Play / Resume / Watch-again buttons.
- **Text-to-speech** — the `elevenlabs` plugin provides high-quality cloud-hosted synthesized voices; the `kokoro` plugin provides a local alternative (no API key, runs entirely in-process). Either backend works for announcements, greetings, and any AI-generated spoken output.
- **Speech-to-text** — the bundled `local_whisper` backend (faster-whisper, no API key) transcribes audio files and browser-mic streams. Extensible via the multi-backend aggregator: the `openai`, `groq`, and `elevenlabs` plugins add batch transcription backends; the `deepgram` and `elevenlabs` plugins add streaming backends; the `porcupine` and `openwakeword` plugins add wake-word detection backends.
- **Outbound phone calls** — the `phone` plugin (carrier-agnostic `PhoneCallService`) places PSTN calls on the user's behalf via a chosen `TelephonyBackend` (the `telnyx` plugin ships in std-plugins). The core `voice_brain` engine drives the live conversation — STT → LLM → TTS, opening disclosure required by federal AI-call rules, per-turn transcript posted to the originating chat, mid-call "intervene with a directive" from the `/calls` SPA page, hard cap on call duration. Slash command `/call <number> <brief>`; AI tool `make_phone_call`. One active call per user.
- **Bidirectional text messaging** — the `messaging` plugin (carrier-agnostic `MessagingService`) sends and receives messages across three transport tiers via a chosen `MessagingBackend` (the `telnyx` plugin contributes one). The default tier is **RCS** (Rich Communication Services — typed text, read receipts, media, no segment length limit, end-to-end encryption with Universal Profile 2.0+, supported by Android Messages, Google Messages, and iOS 18+); the carrier downgrades to **MMS** (media + recipient not RCS-capable) or **SMS** (plain-text fallback, 160-char-per-segment) as needed. The actual tier the carrier picked round-trips into `Message.type` so the SPA renders a per-message badge — useful for debugging "why isn't this thread showing read receipts?". The operator can force a different default in `/settings → Messaging → default_message_type` (e.g. `sms` when your carrier prices RCS differently). Inbound messages route to the configured owner-user and persist as threads; the `/messages` SPA page renders them as a familiar two-pane conversation view with a live compose box. Optional `auto_reply` flag routes inbound messages through `AIService.chat()` so Gilbert can text back on the user's behalf — tagged with `source="messaging"` so the AI's history doesn't pollute the chat sidebar. Slash command `/msg send <number> <body>`; AI tool `send_text_message` (optional `message_type` arg to force a tier). Webhook lands at `/api/telnyx/messages/webhook`.
- **Voice agent (browser)** — the `voice-agent` plugin runs the same `voice_brain` engine over the browser microphone instead of a carrier. Press "Start Conversation" on `/voice` (or trigger a wake-word) and the LLM gets the full Gilbert tool ecosystem during a turn — knowledge search, MCP tools, agent dispatch, scheduler. Toggleable per service; disabled by default until a mic source is wired.
- **Coding-agent conduit** — the `code-conduit` plugin makes Gilbert a *bidirectional messenger* between the user (typically over voice via the Mentra glasses) and a coding agent the user runs on their own machine — currently [OpenCode](https://github.com/sst/opencode) via its `opencode serve` HTTP API; Claude Code planned. Gilbert never writes or reviews code — it just relays messages verbatim in both directions. **Outbound**: "tell Claude to add error handling to the auth flow" → fire-and-forget POST to OpenCode → "Sent to opencode on the gilbert project". **Inbound**: the conduit runs an SSE consumer on OpenCode's event stream and republishes each event as a `code.notification` bus event with a severity bucket (`done` / `error` / `attention` / `info`); voice-brain / Mentra / push-notification subscribers decide their own routing (interrupt TTS for errors and clarification requests, wait for quiet on "done" notifications, silent on info-grade tool calls). AI tools `code_send` (fire a message) and `code_recent_activity` (summarize what the agent's been doing) cover the conversational surface; slash equivalents `/code send` and `/code recent`. Operator-configured project aliases let the user say "the gilbert project" instead of pasting absolute paths.
- **Email inbox** — multi-mailbox, multi-user. Every mailbox is owned by a user and can be shared with individual users or roles for full read/send access. Messages land in a per-mailbox persisted store; outbound drafts queue through a shared outbox with crash-resilient delayed sends. The `google` plugin's Gmail backend is the reference implementation — add more by implementing `EmailBackend`. Incoming mail can also be handed off to an AI chat loop via the Inbox AI Chat service.
- **Calendar** — multi-account, multi-user. Every calendar account is owned by a user and can be shared with individual users or roles. The Calendar service runs one backend instance per `poll_enabled` account, caches events for fast `get_schedule` / `next_event` / `find_free_time`, and emits `calendar.event.upcoming` notifications. Eight AI tools (`list_calendar_accounts`, `get_schedule`, `next_event`, `get_event`, `find_free_time`, `create_event`, `update_event`, `delete_event`) handle every common use case; the three mutating tools default to a preview/confirm `UIBlock` flow so the AI can never silently fire real invite emails. The `google` plugin's Google Calendar backend is the reference implementation.
- **RSS / news feeds + daily briefing** — multi-feed, multi-user. Every feed subscription is owned by a user and can be shared with individuals or roles. The Feeds service runs one backend instance per `poll_enabled` feed (cold-start jitter, source-suggested cadence honored, conditional-GET `etag` / `If-Modified-Since`, body-size cap, graceful give-up at 20 consecutive failures), scores each item against a configurable AI prompt on a bounded async worker pool, optionally ingests article bodies into the knowledge base for vector search (with robots.txt + paywall + SSRF guards and a per-user-per-day budget), and exposes eight AI tools (`news_briefing`, `search_feeds`, `summarize_feed`, `subscribe_feed`, `unsubscribe_feed`, `list_feeds`, `read_feed_item`, `recommend_knowledge_ingestion`). The `subscribe_feed` / `unsubscribe_feed` tools route through a Confirm/Cancel UI block so the AI can't subscribe you to a hallucinated URL. The built-in `RssAtomFeedBackend` (using `feedparser`) parses any RSS 2.0 / Atom 1.0 source — provider-specific backends (Reddit, HackerNews, podcasts) slot in as plugins. The companion `feed_briefing` service fans out a daily presence-fallback briefing event so the greeting service can splice today's top stories into the morning arrival announcement.
- **Tasks / todos** — multi-list, multi-user. Every task list is owned by a user and can be shared with individuals or roles. The Tasks service runs one backend instance per list (the local backend is the source of truth for vendor-free lists; external backends like Google Tasks poll on a per-list schedule with `updatedMin` deltas), persists tasks in a single cross-backend `tasks` collection so `due_today` / `overdue` aggregate across every list a user can see, and uses **local-first reconciliation** for writes — every mutation lands in storage immediately with `sync_status=pending_push`, the upstream push happens inline, and a recurring `tasks-sync-tick` retries pending rows. Ten AI tools (`task_lists`, `add_task`, `get_task`, `list_tasks`, `complete_task`, `update_task`, `cancel_task`, `delete_task`, `tasks_due`, `summarize_today`) cover every common use case; `delete_task` defaults to a soft-delete with UIBlock confirm (recoverable until `retention_days` elapses), and `add_task` returns a UIBlock select when the user has multiple lists with no default. Inbox-AI gains `add_task` for free — the AI lifts action items out of incoming email and uses the `Message-Id` as the idempotency key so re-deliveries don't double-add. Per-user time zones (via `UserContext.tz`) drive day-boundary math so a Pacific user gets *their* "today," not the host's. The `google` plugin's Google Tasks backend ships in v1; Todoist and CalDAV are sketched in the spec for v1.1 and slot in without core changes.
- **Personal health metrics** — multi-backend, multi-user. The Health service aggregates data from `apple-health` (iOS Shortcut webhook), `withings` (OAuth 2.0 pull every 6 hours), and `hk-webhook` (generic catch-all webhook for Garmin Connect IQ widgets, Home Assistant automations, custom scripts) into a single per-user metrics store. Privacy posture is **PHI-adjacent**: owner-only reads enforced in the service (not in routes); cross-user reads gated behind a dedicated `health-admin` role (not auto-granted, not the same as `admin`), audit-logged with target-user notification. Webhook tokens are SHA-256 hash-at-rest with `hmac.compare_digest` confirmation; OAuth state is server-side-bound to the initiating user (defeats confused-deputy), backend-namespaced, expiry-bound, and one-shot consumed. Right-to-delete is a two-step preview-then-confirm wizard requiring the literal `"DELETE"` string; the cascade revokes upstream OAuth grants before local cleanup and writes a `health_audit` row that survives the delete. The hourly TZ-aware daily-summary tick uses `zoneinfo` for DST-correct boundaries (spring-forward 23h, fall-back 25h by construction) and feeds a non-clinical AI summary plus a code-driven flag vocabulary (`low_sleep`, `sedentary`, `weight_drift`) that the proposals integration consumes via the existing observation pipeline. The greeting integration receives a structured `GreetingBrief` snapshot (NOT a pre-canned paragraph) so the greeting model reasons about facts in its own voice, subject to the same forbidden-word rules as the daily-summary prompt.
- **Personalized greetings** — When a known user arrives (via the Presence service), Gilbert generates a 1-2 sentence personalized welcome via the AI, then announces it over speakers. The Greeting service is extensible via the **GreetingContextProvider** capability — any service can advertise `"greeting_context"` and contribute a labeled prose fact that enriches the greeting prompt. The bundled Weather, Feed Briefing, and Health services each contribute context; users control which providers are active via a toggle-per-provider settings page. Installing a new plugin that implements this capability is plug-and-play with no greeting-service edits. The user's custom arrival-greeting prompt template decides what to mention; the AI sees all available context facts and integrates them based on the prompt's instructions.
- **Knowledge base** — index local files (built-in `local_documents` backend) and Google Drive folders (`google` plugin) into a ChromaDB vector store for semantic search.
- **Web search** — the `tavily` plugin surfaces a `/web search`, `/web images`, and `/web fetch` command set for up-to-date answers grounded in real results.
- **Weather** — the Weather service exposes `current_weather`, `forecast`, `weather_alerts`, and `geocode_location` AI tools (plus slash-only `/weather set_home` and `/weather set_units`). The default `open-meteo` plugin needs no API key and covers global current + hourly + daily forecasts. The interface accommodates NWS (US severe-weather alerts) and OpenWeatherMap as future plugins without breaking changes.
- **External notification fan-out** — every notification persisted by the Notifications service is also dispatched, on a per-user opt-in basis, to external push providers via the `push_notifications` service. Users add "notification routes" on `/account/notifications` (`ntfy`, `pushover`, `discord-webhook`, and `telegram` plugins ship out of the box) with per-route urgency floors, source allow/deny lists, and tz-aware quiet hours. The fan-out runs through a bounded queue + worker pool so a slow provider can never back-pressure the in-app dispatcher; URGENT-failure exhaustion escalates to an in-app `push_failure` notification so operators see drops without re-implementing alerting.
- **OCR** — the `tesseract` plugin extracts text from images locally (no network, no API key) for document indexing and vision workflows.
- **Public tunnel** — the `ngrok` plugin provides a public HTTPS URL so OAuth callbacks (Google login, Slack Socket Mode) work behind NAT.
- **Slack bridge** — the `slack` plugin connects a Socket Mode bot so users can chat with Gilbert from Slack DMs and mentions, with the same tool access as the web UI.
- **MCP (Model Context Protocol)** — Gilbert is both an **MCP client** (connect to external MCP servers, per-server RBAC, OAuth 2.1 support, stdio + streamable HTTP + SSE transports, with the external tools merged into Gilbert's own AI pipeline) and an **MCP server** (expose Gilbert's own tools to external agents like Claude Desktop or Cursor over a bearer-authenticated endpoint at `/api/mcp`, with per-client owner identity and AI profile filtering).
- **Remote screens** — push content (PDFs, images, HTML) to browser-based displays via SSE (core).
- **Personalized greetings, roasts, scheduled jobs, RBAC, interactive tool forms** — all core services.
- **AI usage reporting** — every AI round's token consumption (input / output / cache creation / cache read) and USD cost is recorded to the `ai_token_usage` entity collection. Per-round and per-turn totals render inline in chat; an admin-only `/usage` page groups by user / backend / model / profile / tool / date with filterable bar and area charts.
- **Plugin system** — add runtime integrations from any GitHub URL via `/plugin install`, with automatic dependency resolution through the uv workspace. Plugins that need new Python packages trigger a supervised restart; plugins without extra deps hot-load immediately.
- **Self-improvement proposals** — Gilbert observes his own usage (events, conversations, in-chat notes) and on a schedule asks his most capable AI profile to propose concrete improvements: new plugins, new core services, or config tweaks that would close the gaps he just saw. Each proposal lands in the admin-only `/proposals` page with a self-contained "implementation prompt" — paste it into a fresh Claude Code session and it has everything needed to build the thing. Gilbert can read his own source tree (read-only, path-allowlisted) while reflecting, so suggestions are grounded in what's actually there. He's biased toward additive changes (new plugins) over edits to his core code; an opt-in `allow_core_modifications` flag lets him propose deeper changes when you trust him to.

## Architecture

### Interface-First Design

Every component in Gilbert is defined as a Python ABC (abstract base class) with one or more concrete implementations. The core never depends on a specific integration — it depends on the interface. All vendor-specific implementations live in the [gilbert-plugins](https://github.com/briandilley/gilbert-plugins) submodule; core ships only with vendor-free backends (local auth, local filesystem documents, local + browser speaker playback, local Whisper speech-to-text, MCP transports).

```
Interface (core)     →  Implementation (plugin)
────────────────────────────────────────────────
AIBackend            →  anthropic plugin → AnthropicAI (Claude)
                        openai plugin    → OpenAIAI (GPT)
                        qwen plugin      → QwenAI (Alibaba DashScope)
                        deepseek plugin  → DeepSeekAI (V3 + R1)
                        groq plugin      → GroqAI (Llama/Qwen/Mixtral on LPUs)
                        mistral plugin   → MistralAI (La Plateforme)
                        xai plugin       → XAIAI (Grok 4 / 3 / 2 Vision)
                        openrouter plugin → OpenRouterAI (~200 models, multi-provider)
                        ollama plugin    → OllamaAI (local / self-hosted open-weights)
                        gemini plugin    → GeminiAI (Google Gemini 2.5)
                        bedrock plugin   → BedrockAI (AWS Bedrock Converse API)
VisionBackend        →  anthropic plugin → AnthropicVision
TTSBackend           →  elevenlabs plugin → ElevenLabsTTS
BatchTranscriptionBackend → core (local_whisper / faster-whisper, no API key)
                        openai plugin    → OpenAIWhisperBackend (whisper-1 / gpt-4o-transcribe)
                        groq plugin      → GroqWhisperBackend (whisper-large-v3 family)
                        elevenlabs plugin → ElevenLabsScribeBackend (scribe_v1, diarization)
StreamingTranscriptionBackend → elevenlabs plugin → ElevenLabsScribeLiveBackend (WebSocket)
                        deepgram plugin  → DeepgramBackend (Nova-3 WebSocket)
WakeWordBackend      →  porcupine plugin → PorcupineBackend (Picovoice, custom .ppn)
                        openwakeword plugin → OpenWakeWordBackend (local ONNX, no API key)
SpeakerBackend       →  core (LocalSpeaker, BrowserSpeaker) + sonos plugin → SonosSpeaker
MusicBackend         →  sonos plugin → SonosMusic (Spotify via Sonos)
PresenceBackend      →  unifi plugin → UniFiPresenceBackend (Network + Protect + Access)
DoorbellBackend      →  unifi plugin → UniFiDoorbellBackend
CameraEventBackend   →  frigate plugin → FrigateCameraBackend (MQTT push + HTTP snapshots/clips)
EmailBackend         →  google plugin → GmailBackend
CalendarBackend      →  google plugin → GoogleCalendarBackend
TaskBackend          →  core (LocalTaskBackend) + google plugin (GoogleTasksBackend)
HealthBackend        →  apple-health plugin → AppleHealthBackend (push via iOS Shortcut)
                        withings plugin     → WithingsBackend (OAuth pull)
                        hk-webhook plugin   → HKWebhookBackend (generic catch-all push)
DocumentBackend      →  core (LocalDocuments) + google plugin (GDriveDocuments)
AuthBackend          →  core (LocalAuth) + google plugin (GoogleAuthBackend)
UserProviderBackend  →  google plugin → GoogleDirectoryBackend
WebSearchBackend     →  tavily plugin → TavilySearch
WeatherBackend       →  open-meteo plugin → OpenMeteoWeather
OCRBackend           →  tesseract plugin → TesseractOCR
TunnelBackend        →  ngrok plugin → NgrokTunnel
MCPBackend           →  core (stdio, http, sse, browser — consume external MCP servers)
StorageBackend       →  core → SQLiteStorage
```

Want to add support for a different speaker system, AI provider, or presence detector? Implement the interface in a new plugin (or an existing one) and Gilbert picks it up through the backend registry on the next boot. Callers never change.

### Service Manager

Services are the building blocks of Gilbert. Each service declares its **capabilities** (what it provides) and **dependencies** (what it needs), and the service manager handles lifecycle, ordering, and discovery.

```python
class GreetingService(Service):
    def info(self) -> ServiceInfo:
        return ServiceInfo(
            name="greeting",
            capabilities=frozenset({"greeting", "ai_tools"}),
            dependencies=frozenset({"speaker_control", "presence", "tts"}),
        )
```

Services are started in dependency order and stopped in reverse. Any service can discover others at runtime through capability queries — no hardcoded references.

### Event Bus

Services communicate through a publish-subscribe event bus with pattern matching. When someone arrives home, the presence service publishes `presence.arrived`. The greeting service hears it and welcomes them.

```
presence.arrived  →  GreetingService (personalized welcome)
doorbell.ring     →  DoorbellService (announce visitor on speakers)
email.received    →  InboxAIChatService (AI processes the email)
```

This decoupled design means new services can react to existing events without modifying the publishers.

### AI and Tool System

Gilbert's AI service runs an agentic tool-use loop. Services that implement the `ToolProvider` protocol automatically expose their capabilities as AI-callable tools. The AI can chain multiple tools in a single conversation turn — search for a song, play it on a specific speaker group, and announce it over TTS, all from one natural language request.

**AI context profiles** control which tools are available for different interaction types. A sales agent profile might only see the `sales_lead` tool, while a human chat profile sees everything except sales tools. Profiles are managed at runtime under **Security → AI Profiles** or via AI tools themselves.

Tools are filtered through two layers:
1. **Profile filtering** — which tools are available for this type of interaction
2. **RBAC filtering** — which tools this user's role is allowed to invoke

### MCP (Model Context Protocol)

Gilbert sits on both sides of MCP:

- **As an MCP client**, Gilbert connects out to external MCP servers (stdio subprocesses, HTTP streamable, or SSE) and merges their tools into its own agentic pipeline. Servers are configured per-user with a `scope` (private / shared / public) plus an optional `allowed_users` list; a supervisor loop reconnects with exponential backoff; OAuth 2.1 with dynamic client registration handles authenticated servers; each external server can optionally be allowed to request *sampling* (completions from Gilbert's AI) under a named profile with a token budget cap.
- **As an MCP server**, Gilbert exposes its own tools at `/api/mcp` for external agents (Claude Desktop, Cursor, etc.). Admins register client tokens under **MCP → Clients**, each bound to an owner user and an AI profile. Tool discovery and invocation run under the owner's `UserContext` through the exact same profile + RBAC pipeline as chat, so an external agent never sees more than what its owner could. The default `mcp_server_client` profile ships empty (include-mode with zero tools) — new clients can authenticate but call nothing until an admin adds tools to the profile, so the fail-safe for untrusted integrations is "no access."
- **Browser-bridged local servers.** Gilbert can also consume MCP servers running on a user's own machine without any tunnel or inbound firewall hole. A user configures `{slug, name, url}` entries under **MCP → Local** (stored in browser localStorage — the URL never reaches the server), and on every WebSocket connect the tab announces the available slugs. When the AI calls a tool from one of those servers, Gilbert sends an `mcp.bridge.call` frame over the authenticated WebSocket; the browser proxies the JSON-RPC body to the local URL via `fetch` and streams the response back. These session-ephemeral servers live in a per-user in-memory registry, are strictly private to the owner (invisible even to admins), and are torn down the moment the tab disconnects — the perfect shape for personal tools a user wants available only during an active session.

Both sides honour the same "if you can see it, you can use it" principle and the same multi-user ownership model.

### Interactive Tool Forms

Tools can push structured forms directly into the chat UI — text inputs, dropdowns, radio buttons, checkboxes, range sliders, and button groups. A tool returns a `ToolOutput` instead of a plain string, and the form renders inline after the AI's response. When the user submits, the values flow back through the normal conversation as a structured message the AI can process.

This enables richer workflows without leaving the chat: configuring settings, confirming actions, picking from options, or filling out multi-field forms — all driven by tools, not by the AI generating markup.

### WebSocket Protocol

Gilbert exposes a bidirectional WebSocket at `/ws/events` that serves as the primary real-time channel for the web UI, inter-Gilbert communication, and external integrations.

All messages are JSON frames with a `type` field using `namespace.resource.verb` naming:

```
gilbert.event        — server pushes bus events to clients
gilbert.welcome      — sent after auth with user identity and roles
gilbert.sub.add      — subscribe to event patterns (glob matching)
gilbert.sub.remove   — unsubscribe from patterns
gilbert.ping/pong    — heartbeat (30s interval)
gilbert.peer.publish — peer instances publish events to the local bus
chat.message.send    — send a chat message (RPC with .result response)
chat.form.submit    — submit a UI form
chat.history.load    — load conversation as turn-grouped history
```

Events are filtered server-side through three layers: pattern matching (client subscribes to what it cares about), role-based visibility (admin/user/everyone tiers), and content filtering (chat membership and message visibility).

Authentication supports both session cookies (web UI) and bearer tokens via query parameter (peers and integrations). Peer Gilbert instances can authenticate, subscribe to filtered events, and publish events that propagate to the local bus.

### Storage

Gilbert uses a generic entity store — not raw SQL tables. Entities are stored as typed documents with indexes and foreign keys, all through an abstract `StorageBackend` interface. The default implementation is SQLite, but the interface is designed to be swappable. New entity types require no migrations.

## Integrations

Every third-party integration is a plugin in the [gilbert-plugins](https://github.com/briandilley/gilbert-plugins) repository, included as a git submodule at `std-plugins/`. The full inventory — with each plugin's backend names, third-party deps, configuration keys, and slash commands — lives in [`std-plugins/README.md`](std-plugins/README.md). At a glance:

| Plugin | What it adds |
|---|---|
| **anthropic** | Claude AI and Vision backends (default for chat and image understanding) |
| **arr** | Radarr + Sonarr services for movie/TV library management from chat |
| **deepgram** | Deepgram Nova streaming speech-to-text backend |
| **elevenlabs** | High-quality TTS + Scribe batch and streaming speech-to-text backends |
| **frigate** | Frigate NVR object-detection events via MQTT, plus snapshot/clip retrieval over HTTP |
| **google** | OAuth login, Workspace directory sync, Gmail backend, Google Drive documents, Google Calendar, Google Tasks |
| **groq** | Groq LPU chat backend + Groq Whisper batch speech-to-text backend |
| **guess-that-song** | Multiplayer music guessing game managed by the AI |
| **jellyfin** | Jellyfin media library + casting — `MediaLibraryBackend` parity with Plex |
| **ngrok** | Public HTTPS tunnel for OAuth callbacks and webhooks |
| **ntfy / pushover / discord-webhook / telegram** | External notification fan-out backends |
| **open-meteo** | Weather backend (no API key) — current, hourly, and daily forecasts |
| **openai** | OpenAI GPT chat backend + Whisper batch speech-to-text backend |
| **openai-compatible** | Vendor-neutral Chat Completions backend for vLLM, LM Studio, corporate proxies, and other endpoints without a dedicated plugin |
| **openwakeword** | Local wake-word detection (ONNX models, no API key required) |
| **plex** | Plex Media Server library + casting — search, recently-added, on-deck, episode resolution, playback dispatch via the `media_library` service |
| **porcupine** | Picovoice Porcupine wake-word detection (built-in and custom keywords) |
| **slack** | Socket Mode bot bridging Slack DMs/mentions to the AI service |
| **sonos** | Sonos speaker control and Sonos-linked music service access |
| **tavily** | Web search backend with AI-generated summaries |
| **tesseract** | Local OCR (offline, no API key) for document indexing |
| **unifi** | UniFi Network + Protect + Access aggregated into presence and doorbell backends |

Configuration for every plugin is done through the Gilbert Settings UI at **System → Settings** (`/settings`) — no file editing needed. The Settings UI reads each plugin's `config_params()`, renders type-appropriate inputs (with sensitive values masked), and persists changes to entity storage. See [`std-plugins/README.md`](std-plugins/README.md) for each plugin's configuration keys.

### ChromaDB

Vector database for the knowledge base (core dependency). Documents from local files and Google Drive are chunked, embedded, and indexed for semantic search. The AI uses this to answer questions grounded in your actual documents. ChromaDB runs in-process via the `chromadb` Python package — no external service required.

## Plugins

Plugins extend Gilbert without modifying core code. A plugin is a directory containing a `plugin.yaml` manifest, a `plugin.py` entry point with a `create_plugin()` factory, its own `pyproject.toml` declaring third-party Python deps, and the actual integration source. Gilbert loads every plugin discovered under the configured plugin directories at startup.

See [`std-plugins/README.md`](std-plugins/README.md) for the full template and a walkthrough of adding a new plugin.

### Plugin directories

Gilbert scans three directories at startup, each with a distinct role:

| Directory | Purpose | Tracked in gilbert? |
|---|---|---|
| `std-plugins/` | First-party plugins from [`briandilley/gilbert-plugins`](https://github.com/briandilley/gilbert-plugins). Included as a **git submodule**. | Submodule pointer |
| `local-plugins/` | Plugins you write yourself for this installation. | No (gitignored) |
| `installed-plugins/` | Plugins installed at runtime via `/plugin install <url>`. | No (gitignored) |

Every subdirectory of each of these that contains a `plugin.yaml` is loaded at startup. Every plugin also must carry its own `pyproject.toml` — those are all uv workspace members of the root Gilbert project, so a single `uv sync` resolves and installs every plugin's third-party Python deps into the shared venv.

### `std-plugins` as a submodule

First-party plugins live in a separate repository so they can be versioned, reviewed, and released independently of Gilbert's core. Cloning Gilbert fresh:

```bash
git clone https://github.com/briandilley/gilbert.git
cd gilbert
git submodule update --init --recursive   # populates std-plugins/
uv sync                                    # installs core + every plugin's deps
```

Or just run `./gilbert.sh start` — the launcher script auto-runs `git submodule update --init --recursive` if `std-plugins/` is empty, then `uv sync`, then boots Gilbert.

New integrations should be opened as pull requests against the `gilbert-plugins` repo, not this one. Plugin PRs should include the plugin directory complete with `plugin.yaml`, `plugin.py`, `pyproject.toml`, backend source, and tests, plus an updated entry in `std-plugins/README.md` so the plugin inventory stays accurate — see the existing plugins for the pattern.

### Runtime plugin install

Admins can install plugins at runtime from any GitHub URL via the `/plugin install <url>` slash command (or the `plugins.install` WebSocket RPC). Gilbert fetches the plugin into `installed-plugins/<name>/`, validates it, and either:

- **Hot-loads it immediately** if the plugin's `pyproject.toml` declares no third-party Python dependencies (nothing new for the venv to resolve), or
- **Defers loading until restart** if it has deps. In that case, run `/plugin restart` to trigger Gilbert's supervised restart loop — `gilbert.sh` re-runs `uv sync` (which picks up the new workspace member), relaunches Gilbert, and the boot-time loader imports the plugin with its deps now available.

Uninstall with `/plugin uninstall <name>`; list with `/plugin list`.

## Web UI

Gilbert includes a React SPA with pages for chat, inbox, MCP administration, security (users / roles / tool permissions / AI profiles / collection ACLs / event visibility / RPC permissions), and system operations (settings / scheduler / entity browser / plugins / service inspector / AI usage reporting). **All data operations use the WebSocket protocol** — the only HTTP endpoints are authentication (OAuth callbacks, login), the raw ASGI MCP endpoint (`/api/mcp`), and static file serving. The SPA connects to `/ws/events` on load and communicates exclusively via typed RPC frames.

Top-level navigation is organized into dropdown groups (Chat · Inbox · MCP · Security · System) that render as a horizontal nav on desktop and a drawer on mobile. Menu items are filtered per-user by the `dashboard.get` RPC so users only see what they can actually access — e.g. non-admins don't see Security or System at all, and users without the `mcp_server` capability enabled don't see **MCP → Clients**. Clicking a parent group lands on its default child (Users for Security, Settings for System, Servers for MCP).

## Development

```bash
# Install dev tools (ruff, mypy, pytest-cov)
uv sync --extra dev

# Run all tests — includes every std-plugin's tests via pyproject.toml testpaths
uv run pytest

# Run tests with coverage
uv run pytest --cov=gilbert

# Run tests for a single plugin
uv run pytest std-plugins/tesseract/tests/ -v

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/ tests/

# Formatting
uv run ruff format src/ tests/
```

Plugin tests live inside each plugin's `tests/` directory and are collected automatically because `pyproject.toml` lists `std-plugins`, `local-plugins`, and `installed-plugins` in `testpaths`. When adding a new plugin, put its tests under `<plugin>/tests/` and create a `conftest.py` that registers the plugin directory as a Python package for pytest — see `std-plugins/tesseract/tests/conftest.py` for the single-module case or `std-plugins/unifi/tests/conftest.py` for a multi-module plugin with relative imports.

See [CLAUDE.md](CLAUDE.md) for full architecture documentation, design decisions, and development guidelines. See [`std-plugins/CLAUDE.md`](std-plugins/CLAUDE.md) for plugin-specific conventions.

## License

MIT
