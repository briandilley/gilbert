# Gilbert

An AI-powered assistant for home and business automation. Gilbert combines a modular, interface-driven architecture with an agentic AI core — giving it the ability to control speakers, greet people at the door, manage email, spin up a radio DJ, and much more, all orchestrated through natural conversation or automated event-driven workflows.

Everything in Gilbert is an abstraction. Swap your AI provider, your speaker system, your presence detector, or your storage backend without touching a single line of business logic. Add entirely new capabilities through plugins loaded from local directories or GitHub URLs.

## What Can It Do?

Out of the box, Gilbert ships with integrations and services for:

- **AI chat** with tool use — ask Gilbert to play music, check who's home, search your documents, compose an email, or push content to a wall-mounted display
- **Presence detection** — know who's home (and where) via WiFi clients, cameras with facial recognition, and zone-based tracking
- **Doorbell monitoring** — detect ring events and announce visitors over your speakers with a custom TTS voice
- **Music and radio** — search and play tracks via Spotify on Sonos speakers, or let the Radio DJ pick genres based on who's in the room
- **Text-to-speech** — generate speech with ElevenLabs voices for announcements, greetings, and roasts
- **Email inbox** — poll Gmail, search threads, compose and send replies — or let the AI handle incoming messages autonomously
- **Knowledge base** — index local files and Google Drive folders into a vector store (ChromaDB) for semantic search
- **Remote screens** — push content (PDFs, images, HTML) to browser-based displays via SSE
- **Personalized greetings** — welcome people by name when they arrive, with a voice and personality you define
- **Scheduled jobs** — cron-style recurring tasks, one-shot timers, and system maintenance jobs
- **Role-based access control** — per-tool and per-collection permissions with a role hierarchy
- **Interactive tool forms** — tools can push structured forms (inputs, selects, sliders, buttons) into the chat for user interaction
- **Plugin system** — extend Gilbert with new services, tools, and integrations without modifying core code

## Architecture

### Interface-First Design

Every component in Gilbert is defined as a Python ABC (abstract base class) with one or more concrete implementations. The core never depends on a specific integration — it depends on the interface.

```
AIBackend           →  AnthropicAI (Claude)
SpeakerBackend      →  SonosSpeaker
MusicBackend        →  SpotifyMusic
TTSBackend          →  ElevenLabsTTS
PresenceBackend     →  UniFi (Network + Protect)
DoorbellBackend     →  UniFi Protect
EmailBackend        →  Gmail
DocumentBackend     →  Local filesystem, Google Drive
AuthProvider        →  Local passwords, Google OAuth
StorageBackend      →  SQLite
UserProviderService →  Google Workspace Directory
```

Want to add support for a different speaker system, AI provider, or presence detector? Implement the interface and register it. Callers don't change.

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

**AI context profiles** control which tools are available for different interaction types. A sales agent profile might only see the `sales_lead` tool, while a human chat profile sees everything except sales tools. Profiles are managed at runtime through the web UI or AI tools themselves.

Tools are filtered through two layers:
1. **Profile filtering** — which tools are available for this type of interaction
2. **RBAC filtering** — which tools this user's role is allowed to invoke

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
chat.form.submit     — submit a UI form
chat.history.load    — load conversation messages
```

Events are filtered server-side through three layers: pattern matching (client subscribes to what it cares about), role-based visibility (admin/user/everyone tiers), and content filtering (chat membership and message visibility).

Authentication supports both session cookies (web UI) and bearer tokens via query parameter (peers and integrations). Peer Gilbert instances can authenticate, subscribe to filtered events, and publish events that propagate to the local bus.

### Storage

Gilbert uses a generic entity store — not raw SQL tables. Entities are stored as typed documents with indexes and foreign keys, all through an abstract `StorageBackend` interface. The default implementation is SQLite, but the interface is designed to be swappable. New entity types require no migrations.

## Integrations

### Anthropic (Claude)

The AI backend. Powers all natural language interactions, tool orchestration, email processing, greeting generation, music selection, and more. Supports streaming responses and automatic token tracking.

### UniFi

Two integrations in one:

- **UniFi Network** — polls WiFi client lists to detect which devices (and therefore which people) are on the network, enabling zone-based presence tracking
- **UniFi Protect** — monitors camera feeds for doorbell ring events and facial recognition, feeding into the presence and doorbell services

### Sonos

Discovers speakers on the local network via UPnP. Supports playback, volume control, and speaker grouping for synchronized multi-room audio. Used by the music service, Radio DJ, TTS announcements, and doorbell notifications.

### Spotify

Music search, track/album/playlist metadata, and playable URIs. The Radio DJ uses it to build context-aware playlists, and the music service exposes search and playback as AI tools.

### ElevenLabs

High-quality text-to-speech synthesis with multiple configurable voices. Used for arrival greetings, doorbell announcements, roasts, and any AI-generated spoken output.

### Google Workspace

Multiple Google integrations:

- **Gmail** — email polling, thread tracking, compose and send with domain-wide delegation
- **Google Drive** — document sync and export for the knowledge base
- **Google Directory** — user and group sync from Workspace for the user provider system
- **Google OAuth** — authentication provider for the login system

### Slack

Socket Mode bot integration that routes DMs and channel mentions to the AI service. Users can chat with Gilbert directly in Slack with the same tool access as the web UI.

### ChromaDB

Vector database for the knowledge base. Documents from local files and Google Drive are chunked, embedded, and indexed for semantic search. The AI uses this to answer questions grounded in your actual documents.

## Plugins

Plugins extend Gilbert without modifying core code. A plugin is a Python package that implements the `Plugin` interface and exposes a `create_plugin()` factory function.

```
plugins/
  my-plugin/
    plugin.yaml      # metadata and default config
    __init__.py       # create_plugin() entry point
    service.py        # your service implementation
```

Plugins receive a `PluginContext` with access to the service manager, configuration, a data directory, and namespaced storage (automatically prefixed to avoid collisions). They can register new services, subscribe to events, expose AI tools, and add web routes.

Plugins are loaded from:
- **Local directories** — for development or private plugins
- **GitHub URLs** — fetched and cached at runtime

Configuration follows the same layering as core: plugin defaults in `plugin.yaml`, overrides in `.gilbert/config.yaml` under `plugins.config.<name>`.

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Docker (optional, for dependent services like ChromaDB)

### Clone and Install

```bash
git clone https://github.com/briandilley/gilbert.git
cd gilbert
uv sync
```

### Configure

Gilbert ships with sensible defaults in `gilbert.yaml`. To customize for your installation, create a local override file:

```bash
mkdir -p .gilbert
cp gilbert.yaml .gilbert/config.yaml
# Edit .gilbert/config.yaml with your credentials and settings
```

The `.gilbert/` directory is gitignored — your API keys, database, and logs stay local. Overrides are deep-merged on top of defaults, so you only need to include the values you're changing.

At minimum, you'll want to configure:

```yaml
# .gilbert/config.yaml
ai:
  enabled: true
  credential: my-anthropic-key

credentials:
  my-anthropic-key:
    type: api_key
    api_key: sk-ant-...
```

See `gilbert.yaml` for the full set of configurable services and their options.

### Run

```bash
# Start infrastructure (Docker services like ChromaDB, if needed)
./gilbert.sh infra

# Start Gilbert
./gilbert.sh start

# Stop Gilbert
./gilbert.sh stop
```

On first run, Gilbert creates the `.gilbert/` directory and initializes the SQLite database, log files, and default AI profiles. The web UI is available at `http://localhost:8000`.

## Web UI

Gilbert includes a React SPA with pages for chat, documents, inbox, screens, system, entities, and roles. **All data operations use the WebSocket protocol** — the only HTTP endpoints are authentication (OAuth callbacks, login) and static file serving. The SPA connects to `/ws/events` on load and communicates exclusively via typed RPC frames.

## Development

```bash
# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=gilbert

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/ tests/

# Formatting
uv run ruff format src/ tests/
```

See [CLAUDE.md](CLAUDE.md) for full architecture documentation, design decisions, and development guidelines.

## License

MIT
