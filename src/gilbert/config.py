"""Configuration — loading and validation of Gilbert settings.

Config layering:
1. gilbert.yaml (committed defaults — bootstrap only: storage, logging, web)
2. .gilbert/config.yaml (per-installation overrides — gitignored)

All non-bootstrap configuration is stored in entity storage and managed
via the web UI at /settings. The .gilbert/ directory is auto-created on
first run.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Base directory for per-installation data
DATA_DIR = Path(".gilbert")

# Default config (committed) and override config (gitignored)
DEFAULT_CONFIG_PATH = Path("gilbert.yaml")
OVERRIDE_CONFIG_PATH = DATA_DIR / "config.yaml"

# Sections that must remain in YAML (needed before entity storage exists)
YAML_ONLY_SECTIONS = frozenset({"storage", "logging", "web"})


class BaseConfig(BaseModel):
    """Base for all non-bootstrap config models. Preserves unknown fields."""

    model_config = {"extra": "allow"}


class StorageConfig(BaseModel):
    """Storage backend configuration."""

    backend: str = "sqlite"
    connection: str = ".gilbert/gilbert.db"


class PluginSource(BaseModel):
    """A plugin source — local path or GitHub URL."""

    source: str
    enabled: bool = True


class PluginsConfig(BaseConfig):
    """Plugin system configuration."""

    directories: list[str] = []
    sources: list[PluginSource] = []
    cache_dir: str = ".gilbert/plugin-cache"
    config: dict[str, dict[str, Any]] = {}


class WebSearchConfig(BaseConfig):
    """Web search configuration."""

    enabled: bool = False
    backend: str = "tavily"
    settings: dict[str, Any] = {}


class SkillsConfig(BaseConfig):
    """Skills system configuration."""

    enabled: bool = True
    directories: list[str] = ["./skills"]
    cache_dir: str = ".gilbert/skill-cache"
    user_dir: str = ".gilbert/skills"


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


class TunnelConfig(BaseConfig):
    """Public tunnel configuration (ngrok, etc.)."""

    enabled: bool = False
    backend: str = "ngrok"
    settings: dict[str, Any] = {}


class TTSConfig(BaseConfig):
    """Text-to-speech configuration."""

    enabled: bool = False
    backend: str = "elevenlabs"
    silence_padding: float = 3.0
    settings: dict[str, Any] = {}


class AIConfig(BaseConfig):
    """AI service configuration."""

    enabled: bool = False
    backend: str = "anthropic"
    max_history_messages: int = 50
    max_tool_rounds: int = 10
    settings: dict[str, Any] = {}


class AuthConfig(BaseConfig):
    """Authentication and user management configuration."""

    enabled: bool = True
    default_roles: list[str] = ["user"]
    session_ttl_seconds: int = 86400
    root_password: str = ""
    allow_user_creation: bool = True




class MusicConfig(BaseConfig):
    """Music service configuration."""

    enabled: bool = False
    backend: str = "spotify"
    settings: dict[str, Any] = {}


class UniFiControllerConfig(BaseConfig):
    """Connection config for a single UniFi OS controller."""

    host: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = False


class PresenceConfig(BaseConfig):
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



class KnowledgeLocalConfig(BaseConfig):
    """Local filesystem knowledge backend configuration."""

    enabled: bool = False
    name: str = "local"
    path: str = ""


class KnowledgeGDriveConfig(BaseConfig):
    """Google Drive knowledge backend configuration."""

    enabled: bool = False
    name: str = "gdrive"
    folder_id: str = ""


class KnowledgeConfig(BaseConfig):
    """Document knowledge store configuration."""

    enabled: bool = False
    local: KnowledgeLocalConfig = KnowledgeLocalConfig()
    gdrive: KnowledgeGDriveConfig = KnowledgeGDriveConfig()
    sync_interval_seconds: int = 300
    chunk_size: int = 800
    chunk_overlap: int = 200
    max_search_results: int = 20
    chromadb_path: str = ".gilbert/chromadb"
    vision_enabled: bool = True
    vision_model: str = "claude-sonnet-4-5-20250929"


class DoorbellConfig(BaseConfig):
    """Doorbell monitoring configuration."""

    enabled: bool = False
    backend: str = "unifi"
    poll_interval_seconds: float = 5.0
    speakers: list[str] = []
    settings: dict[str, Any] = {}



class GreetingConfig(BaseConfig):
    """Morning greeting configuration."""

    enabled: bool = False
    start_hour: int = 6
    cutoff_hour: int = 14
    timezone: str = "UTC"
    style: str = ""
    speakers: list[str] = []


class BackupConfig(BaseConfig):
    """Backup service configuration."""

    enabled: bool = False
    retention_days: int = 30
    backup_hour: int = 3
    backup_minute: int = 0


class RadioDJConfig(BaseConfig):
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
    min_switch_interval: int = 15
    default_volume: int = 35
    speakers: list[str] = []
    stop_when_empty: bool = True
    poll_interval: int = 60


class RoastConfig(BaseConfig):
    """Random roast service configuration."""

    enabled: bool = False
    probability: float = 0.10
    ai_prompt: str = "Generate a playful, friendly roast of {name}. Be funny and teasing but never mean or hurtful. Keep it to 1-2 sentences."


class ScreenConfig(BaseConfig):
    """Remote display screen configuration."""

    enabled: bool = False
    tmp_ttl_seconds: int = 1800
    cleanup_interval_seconds: int = 300


class InboxAIChatConfig(BaseConfig):
    """Email-to-AI chat configuration."""

    enabled: bool = False
    allowed_emails: list[str] = []
    allowed_domains: list[str] = []
    required_subject: str = ""


class InboxConfig(BaseConfig):
    """Email inbox configuration."""

    enabled: bool = False
    backend: str = "gmail"
    email_address: str = ""
    poll_interval: int = 60
    max_body_length: int = 50000


class SlackConfig(BaseConfig):
    """Slack integration configuration."""

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""


class SpeakerConfig(BaseConfig):
    """Speaker system configuration."""

    enabled: bool = False
    backend: str = "sonos"
    default_announce_volume: int | None = None
    settings: dict[str, Any] = {}


class GilbertConfig(BaseConfig):
    """Top-level Gilbert configuration."""

    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()
    web: WebConfig = WebConfig()
    plugins: PluginsConfig = PluginsConfig()
    output_ttl_seconds: int = 3600
    tts: TTSConfig = TTSConfig()
    ai: AIConfig = AIConfig()
    auth: AuthConfig = AuthConfig()
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
    websearch: WebSearchConfig = WebSearchConfig()
    skills: SkillsConfig = SkillsConfig()


def load_config(
    path: str | Path | None = None,
    plugin_defaults: dict[str, dict[str, Any]] | None = None,
) -> GilbertConfig:
    """Load configuration with layered overrides.

    1. Start with gilbert.yaml (committed defaults — bootstrap only)
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
