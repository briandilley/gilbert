"""Configuration — loading and validation of Gilbert settings.

Config layering:
1. gilbert.yaml (committed defaults — shipped with the repo)
2. .gilbert/config.yaml (per-installation overrides — gitignored)
3. Explicit path override (if provided)

The .gilbert/ directory is the per-installation data folder. It contains:
- config.yaml (user overrides)
- gilbert.db (SQLite database)
- gilbert.log / ai_calls.log (log files)
- plugins/ (plugin cache)

Users clone the repo and run it. The .gilbert/ folder is auto-created
on first run. No source files need to be edited.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from gilbert.interfaces.credentials import AnyCredential

logger = logging.getLogger(__name__)

# Base directory for per-installation data
DATA_DIR = Path(".gilbert")

# Default config (committed) and override config (gitignored)
DEFAULT_CONFIG_PATH = Path("gilbert.yaml")
OVERRIDE_CONFIG_PATH = DATA_DIR / "config.yaml"


class StorageConfig(BaseModel):
    """Storage backend configuration."""

    backend: str = "sqlite"
    connection: str = ".gilbert/gilbert.db"


class PluginSource(BaseModel):
    """A plugin source — local path or GitHub URL."""

    source: str
    enabled: bool = True


class PluginsConfig(BaseModel):
    """Plugin system configuration."""

    directories: list[str] = []
    sources: list[PluginSource] = []
    cache_dir: str = ".gilbert/plugin-cache"
    config: dict[str, dict[str, Any]] = {}


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    file: str = ".gilbert/gilbert.log"
    ai_log_file: str = ".gilbert/ai_calls.log"
    loggers: dict[str, str] = {}


class WebConfig(BaseModel):
    """Web server configuration."""

    host: str = "0.0.0.0"
    port: int = 8765


class TunnelConfig(BaseModel):
    """Public tunnel configuration (ngrok, etc.)."""

    enabled: bool = False
    credential: str = ""  # name of an api_key credential for ngrok auth token
    domain: str = ""  # custom ngrok domain (e.g., "myapp.ngrok.io")


class TTSVoiceConfig(BaseModel):
    """A named TTS voice mapping."""

    voice_id: str


class TTSConfig(BaseModel):
    """Text-to-speech configuration."""

    enabled: bool = False
    backend: str = "elevenlabs"
    credential: str = ""
    default_voice: str = ""
    voices: dict[str, TTSVoiceConfig] = {}
    settings: dict[str, Any] = {}


class AIProfileConfig(BaseModel):
    """An AI context profile — defines tool access for a named AI interaction."""

    description: str = ""
    tool_mode: str = "all"  # "all" | "include" | "exclude"
    tools: list[str] = []
    tool_roles: dict[str, str] = {}  # per-tool role overrides


class AIConfig(BaseModel):
    """AI service configuration."""

    enabled: bool = False
    backend: str = "anthropic"
    credential: str = ""
    system_prompt: str = "You are Gilbert, an AI assistant for home and business automation."
    max_history_messages: int = 50
    max_tool_rounds: int = 10
    settings: dict[str, Any] = {}
    profiles: dict[str, AIProfileConfig] = {}


class AuthRoleMapping(BaseModel):
    """Maps an external group to a Gilbert role."""

    group: str
    role: str


class AuthProviderConfig(BaseModel):
    """Configuration for a single auth provider."""

    type: str
    enabled: bool = True
    credential: str = ""
    domain: str = ""
    role_mappings: list[AuthRoleMapping] = []
    settings: dict[str, Any] = {}


class AuthConfig(BaseModel):
    """Authentication and user management configuration."""

    enabled: bool = False
    providers: list[AuthProviderConfig] = [AuthProviderConfig(type="local")]
    default_roles: list[str] = ["user"]
    session_ttl_seconds: int = 86400
    root_password: str = ""
    allow_user_creation: bool = True


class GoogleConfig(BaseModel):
    """Google API configuration.

    Supports multiple named credential profiles. Each consumer (directory
    sync, email, etc.) references a profile by name.
    """

    enabled: bool = False
    oauth_credential: str = ""
    accounts: dict[str, "GoogleAccountConfig"] = {}


class GoogleAccountConfig(BaseModel):
    """A named Google service account profile."""

    credential: str  # references a key in top-level credentials
    delegated_user: str = ""
    scopes: list[str] = []


class MusicConfig(BaseModel):
    """Music service configuration."""

    enabled: bool = False
    backend: str = "spotify"
    credential: str = ""
    settings: dict[str, Any] = {}


class UniFiControllerConfig(BaseModel):
    """Connection config for a single UniFi OS controller."""

    host: str = ""
    credential: str = ""
    verify_ssl: bool = False


class PresenceConfig(BaseModel):
    """Presence detection configuration."""

    enabled: bool = False
    backend: str = "unifi"
    poll_interval_seconds: int = 30
    unifi_network: UniFiControllerConfig = UniFiControllerConfig()
    unifi_protect: UniFiControllerConfig = UniFiControllerConfig()
    device_person_map: dict[str, str] = {}
    zone_aliases: dict[str, list[str]] = {}
    face_lookback_minutes: int = 30
    badge_lookback_hours: int = 24
    settings: dict[str, Any] = {}


class DocumentSourceConfig(BaseModel):
    """Configuration for a single document source/backend."""

    type: str  # "local" or "gdrive"
    name: str
    enabled: bool = True
    path: str = ""  # local filesystem path
    account: str = ""  # google account profile name
    folder_id: str = ""  # google drive folder ID
    shared_drive_id: str = ""  # google shared drive ID


class KnowledgeConfig(BaseModel):
    """Document knowledge store configuration."""

    enabled: bool = False
    sources: list[DocumentSourceConfig] = []
    sync_interval_seconds: int = 300
    chunk_size: int = 800
    chunk_overlap: int = 200
    max_search_results: int = 20
    chromadb_path: str = ".gilbert/chromadb"
    vision_enabled: bool = True
    vision_credential: str = ""  # credential name for Vision API (defaults to AI credential)
    vision_model: str = "claude-sonnet-4-5-20250929"


class DoorbellConfig(BaseModel):
    """Doorbell monitoring configuration."""

    enabled: bool = False
    backend: str = "unifi"
    poll_interval_seconds: float = 5.0
    doorbell_names: dict[str, str] = {}  # camera name → friendly door name
    speakers: list[str] = []  # speaker names for announcements (empty = all)
    voice_name: str = ""  # TTS voice name
    unifi_protect: UniFiControllerConfig = UniFiControllerConfig()


class GreetingConfig(BaseModel):
    """Morning greeting configuration."""

    enabled: bool = False
    start_hour: int = 6
    cutoff_hour: int = 14
    timezone: str = "UTC"
    style: str = ""  # custom style instructions for AI greeting generation
    speakers: list[str] = []  # speaker names to announce on (empty = all)
    voice_name: str = ""  # TTS voice name (empty = default)


class BackupConfig(BaseModel):
    """Backup service configuration."""

    enabled: bool = False
    retention_days: int = 30
    backup_hour: int = 3
    backup_minute: int = 0


class RadioDJConfig(BaseModel):
    """Radio DJ service configuration."""

    enabled: bool = False
    default_genres: list[str] = [
        "classic rock",
        "90s hits",
        "blues rock",
        "indie rock",
        "funk",
        "80s hits",
    ]
    min_switch_interval: int = 15  # minutes between auto genre switches
    default_volume: int = 35
    speakers: list[str] = []  # speaker names (empty = all)
    stop_when_empty: bool = True
    poll_interval: int = 60  # seconds


class RoastConfig(BaseModel):
    """Random roast service configuration."""

    enabled: bool = False
    probability: float = 0.10
    ai_prompt: str = "Generate a playful, friendly roast of {name}. Be funny and teasing but never mean or hurtful. Keep it to 1-2 sentences."


class ScreenConfig(BaseModel):
    """Remote display screen configuration."""

    enabled: bool = False
    tmp_ttl_seconds: int = 1800
    cleanup_interval_seconds: int = 300


class InboxAIChatConfig(BaseModel):
    """Email-to-AI chat configuration."""

    enabled: bool = False
    allowed_emails: list[str] = []
    allowed_domains: list[str] = []
    required_subject: str = ""


class InboxConfig(BaseModel):
    """Email inbox configuration."""

    enabled: bool = False
    backend: str = "gmail"
    credential: str = ""
    email_address: str = ""
    poll_interval: int = 60
    max_body_length: int = 50000


class SlackConfig(BaseModel):
    """Slack integration configuration."""

    enabled: bool = False
    bot_credential: str = ""  # Name of api_key credential for bot token
    app_credential: str = ""  # Name of api_key credential for app token


class SpeakerConfig(BaseModel):
    """Speaker system configuration."""

    enabled: bool = False
    backend: str = "sonos"
    default_announce_volume: int | None = None
    settings: dict[str, Any] = {}


class GilbertConfig(BaseModel):
    """Top-level Gilbert configuration."""

    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()
    web: WebConfig = WebConfig()
    credentials: dict[str, AnyCredential] = {}
    plugins: PluginsConfig = PluginsConfig()
    output_ttl_seconds: int = 3600
    tts: TTSConfig = TTSConfig()
    ai: AIConfig = AIConfig()
    auth: AuthConfig = AuthConfig()
    google: GoogleConfig = GoogleConfig()
    tunnel: TunnelConfig = TunnelConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    presence: PresenceConfig = PresenceConfig()
    doorbell: DoorbellConfig = DoorbellConfig()
    greeting: GreetingConfig = GreetingConfig()
    screens: ScreenConfig = ScreenConfig()
    inbox: InboxConfig = InboxConfig()
    inbox_ai_chat: InboxAIChatConfig = InboxAIChatConfig()
    slack: SlackConfig = SlackConfig()
    speaker: SpeakerConfig = SpeakerConfig()
    music: MusicConfig = MusicConfig()
    backup: BackupConfig = BackupConfig()
    radio_dj: RadioDJConfig = RadioDJConfig()
    roast: RoastConfig = RoastConfig()


def load_config(
    path: str | Path | None = None,
    plugin_defaults: dict[str, dict[str, Any]] | None = None,
) -> GilbertConfig:
    """Load configuration with layered overrides.

    1. Start with gilbert.yaml (committed defaults)
    2. Deep-merge plugin default configs (from plugin.yaml files)
    3. Deep-merge .gilbert/config.yaml on top (per-installation overrides)
    4. If an explicit path is given, use only that file instead.

    The .gilbert/ directory is created if it doesn't exist.
    """
    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        logger.info("Loading config from explicit path: %s", config_path)
        return _load_from_file(config_path)

    # Layer 1: committed defaults
    base: dict[str, Any] = {}
    if DEFAULT_CONFIG_PATH.exists():
        logger.info("Loading default config: %s", DEFAULT_CONFIG_PATH)
        base = _load_yaml(DEFAULT_CONFIG_PATH)

    # Layer 2: plugin default configs (namespaced under plugins.config.<name>)
    if plugin_defaults:
        plugin_config_section = base.get("plugins", {})
        if isinstance(plugin_config_section, list):
            # Migrate legacy list format
            plugin_config_section = {"sources": plugin_config_section}
        existing_plugin_config = plugin_config_section.get("config", {})
        # Plugin defaults go first, user overrides win later
        merged_plugin_config = dict(plugin_defaults)
        merged_plugin_config = _deep_merge(merged_plugin_config, existing_plugin_config)
        plugin_config_section["config"] = merged_plugin_config
        base["plugins"] = plugin_config_section

    # Layer 3: per-installation overrides
    if OVERRIDE_CONFIG_PATH.exists():
        logger.info("Loading override config: %s", OVERRIDE_CONFIG_PATH)
        overrides = _load_yaml(OVERRIDE_CONFIG_PATH)
        base = _deep_merge(base, overrides)

    if not base:
        logger.info("No config files found, using defaults")
        return GilbertConfig()

    # Handle legacy plugins list format
    plugins_raw = base.get("plugins")
    if isinstance(plugins_raw, list):
        base["plugins"] = {
            "sources": [
                s if isinstance(s, dict) else {"source": s}
                for s in plugins_raw
            ]
        }

    return GilbertConfig.model_validate(base)


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return raw if isinstance(raw, dict) else {}


def _load_from_file(path: Path) -> GilbertConfig:
    raw = _load_yaml(path)
    if not raw:
        return GilbertConfig()
    return GilbertConfig.model_validate(raw)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base. Override values win."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
