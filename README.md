# Gilbert

An AI-powered assistant for home and business automation. Gilbert combines a modular, interface-driven architecture with an agentic AI core — giving it the ability to control speakers, greet people at the door, manage email, spin up a radio DJ, expose its tools over MCP, and much more, all orchestrated through natural conversation or automated event-driven workflows.

Everything in Gilbert is an abstraction. Swap your AI provider, your speaker system, your presence detector, or your storage backend without touching a single line of business logic. The core ships with only vendor-free backends (local auth, local documents); every third-party integration — Anthropic, Sonos, Google, UniFi, ElevenLabs, Tavily, Slack, ngrok, Tesseract — is a **plugin**. Plugins live in a separate [gilbert-plugins](https://github.com/briandilley/gilbert-plugins) repo that's included as a git submodule at `std-plugins/`, and new plugins can be added at runtime from any GitHub URL.

Gilbert is a **multi-user system from the ground up** — every piece of state (mailboxes, chat history, documents, MCP servers, scheduled jobs) is owned by a specific user, shared via roles and per-collection ACLs, and gated by a role-based access control layer that consistently applies across the web UI, chat, tools, events, and the MCP endpoint.

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package and project manager)
- Git (with submodule support — any recent version)
- Node.js + npm (only if you want to rebuild the frontend; prebuilt assets ship under `src/gilbert/web/spa/`)

Some plugins have additional OS-level prerequisites — e.g. the `tesseract` plugin needs the Tesseract binary (`apt install tesseract-ocr` or `brew install tesseract`). Check [`std-plugins/README.md`](std-plugins/README.md) for per-plugin requirements.

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
web:
  port: 9000
EOF
```

The `.gilbert/` directory is gitignored — your API keys, database, and logs stay local. Runtime config (AI backend selection, TTS API keys, plugin settings, etc.) is managed through the Settings UI, not this file.

At minimum, before Gilbert is useful, open the Settings UI and configure:

- **AI** → select the `anthropic` backend and enter your API key (`sk-ant-…`).
- **Whatever plugins you care about** — e.g. Sonos speakers (discovery is automatic), Google Workspace (OAuth flow), UniFi presence (host + credentials), and so on. Each plugin's settings page has a **Test connection** button to verify credentials before you commit to them.

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

On first run, Gilbert creates the `.gilbert/` directory and initializes the SQLite database, log files, and default AI profiles. The web UI is available at `http://localhost:8000` — log in with the local admin account (bootstrapped automatically on first boot) and head to **Security → Users** to add more accounts.

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
- **Music and speaker control** — the `sonos` plugin discovers Sonos speakers on the LAN, handles playback/volume/grouping, and exposes the Sonos-linked music services (Spotify, Apple Music, etc.) as playable backends for the Music service and Radio DJ.
- **Text-to-speech** — the `elevenlabs` plugin provides high-quality synthesized voices for announcements, greetings, Radio DJ narration, and any AI-generated spoken output.
- **Email inbox** — multi-mailbox, multi-user. Every mailbox is owned by a user and can be shared with individual users or roles for full read/send access. Messages land in a per-mailbox persisted store; outbound drafts queue through a shared outbox with crash-resilient delayed sends. The `google` plugin's Gmail backend is the reference implementation — add more by implementing `EmailBackend`. Incoming mail can also be handed off to an AI chat loop via the Inbox AI Chat service.
- **Knowledge base** — index local files (built-in `local_documents` backend) and Google Drive folders (`google` plugin) into a ChromaDB vector store for semantic search.
- **Web search** — the `tavily` plugin surfaces a `/web search`, `/web images`, and `/web fetch` command set for up-to-date answers grounded in real results.
- **OCR** — the `tesseract` plugin extracts text from images locally (no network, no API key) for document indexing and vision workflows.
- **Public tunnel** — the `ngrok` plugin provides a public HTTPS URL so OAuth callbacks (Google login, Slack Socket Mode) work behind NAT.
- **Slack bridge** — the `slack` plugin connects a Socket Mode bot so users can chat with Gilbert from Slack DMs and mentions, with the same tool access as the web UI.
- **MCP (Model Context Protocol)** — Gilbert is both an **MCP client** (connect to external MCP servers, per-server RBAC, OAuth 2.1 support, stdio + streamable HTTP + SSE transports, with the external tools merged into Gilbert's own AI pipeline) and an **MCP server** (expose Gilbert's own tools to external agents like Claude Desktop or Cursor over a bearer-authenticated endpoint at `/api/mcp`, with per-client owner identity and AI profile filtering).
- **Remote screens** — push content (PDFs, images, HTML) to browser-based displays via SSE (core).
- **Personalized greetings, Radio DJ, roasts, scheduled jobs, RBAC, interactive tool forms** — all core services.
- **AI usage reporting** — every AI round's token consumption (input / output / cache creation / cache read) and USD cost is recorded to the `ai_token_usage` entity collection. Per-round and per-turn totals render inline in chat; an admin-only `/usage` page groups by user / backend / model / profile / tool / date with filterable bar and area charts.
- **Plugin system** — add runtime integrations from any GitHub URL via `/plugin install`, with automatic dependency resolution through the uv workspace. Plugins that need new Python packages trigger a supervised restart; plugins without extra deps hot-load immediately.

## Architecture

### Interface-First Design

Every component in Gilbert is defined as a Python ABC (abstract base class) with one or more concrete implementations. The core never depends on a specific integration — it depends on the interface. All vendor-specific implementations live in the [gilbert-plugins](https://github.com/briandilley/gilbert-plugins) submodule; core ships only with vendor-free backends (local auth, local filesystem documents).

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
SpeakerBackend       →  sonos plugin → SonosSpeaker
MusicBackend         →  sonos plugin → SonosMusic (Spotify via Sonos)
PresenceBackend      →  unifi plugin → UniFiPresenceBackend (Network + Protect + Access)
DoorbellBackend      →  unifi plugin → UniFiDoorbellBackend
EmailBackend         →  google plugin → GmailBackend
DocumentBackend      →  core (LocalDocuments) + google plugin (GDriveDocuments)
AuthBackend          →  core (LocalAuth) + google plugin (GoogleAuthBackend)
UserProviderBackend  →  google plugin → GoogleDirectoryBackend
WebSearchBackend     →  tavily plugin → TavilySearch
OCRBackend           →  tesseract plugin → TesseractOCR
TunnelBackend        →  ngrok plugin → NgrokTunnel
MCPBackend           →  core (stdio, http, sse, browser — consume external MCP servers)
StorageBackend       →  core → SQLiteStorage
```

Want to add support for a different speaker system, AI provider, or presence detector? Implement the interface in a new plugin (or an existing one) and Gilbert picks it up through the backend registry on the next boot. Callers never change.

### Service Manager

Services are the building blocks of Gilbert. Each service declares its **capabilities** (what it provides) and **dependencies** (what it needs), and the service manager handles lifecycle, ordering, and discovery.

```python
class RadioDJService(Service):
    def info(self) -> ServiceInfo:
        return ServiceInfo(
            name="radio_dj",
            capabilities=frozenset({"radio_dj", "ai_tools"}),
            dependencies=frozenset({"music", "speaker_control", "presence"}),
        )
```

Services are started in dependency order and stopped in reverse. Any service can discover others at runtime through capability queries — no hardcoded references.

### Event Bus

Services communicate through a publish-subscribe event bus with pattern matching. When someone arrives home, the presence service publishes `presence.arrived`. The greeting service hears it and welcomes them. The Radio DJ hears it and adjusts the genre to their taste.

```
presence.arrived  →  GreetingService (personalized welcome)
                  →  RadioDJService  (switch genre for this person)
presence.departed →  RadioDJService  (stop if nobody's home)
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
| **elevenlabs** | High-quality TTS for announcements, greetings, Radio DJ narration |
| **google** | OAuth login, Workspace directory sync, Gmail backend, Google Drive documents |
| **guess-that-song** | Multiplayer music guessing game managed by the AI |
| **ngrok** | Public HTTPS tunnel for OAuth callbacks and webhooks |
| **openai** | OpenAI GPT chat backend (configure alongside or instead of anthropic) |
| **openai-compatible** | Vendor-neutral Chat Completions backend for vLLM, LM Studio, corporate proxies, and other endpoints without a dedicated plugin |
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
