"""Application bootstrap — wires everything together and manages lifecycle."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gilbert.config import (
    DATA_DIR,
    DEFAULT_CONFIG_PATH,
    OVERRIDE_CONFIG_PATH,
    GilbertConfig,
    _deep_merge,
    _load_yaml,
)
from gilbert.core.events import InMemoryEventBus
from gilbert.core.logging import setup_logging
from gilbert.core.registry import ServiceRegistry
from gilbert.core.service_manager import ServiceManager
from gilbert.core.services import (
    AuthService,
    EventBusService,
    InboxService,
    MusicService,
    SpeakerService,
    StorageService,
    TTSService,
    UserService,
)
from gilbert.core.services.ai import AIService
from gilbert.core.services.configuration import ConfigurationService
from gilbert.interfaces.events import EventBus
from gilbert.interfaces.plugin import Plugin, PluginContext
from gilbert.interfaces.service import Service
from gilbert.interfaces.storage import StorageBackend, StorageProvider
from gilbert.plugins.loader import PluginLoader, PluginManifest
from gilbert.storage.sqlite import SQLiteStorage

logger = logging.getLogger(__name__)

# Plugin data lives under .gilbert/plugin-data/<plugin-name>/
PLUGIN_DATA_DIR = DATA_DIR / "plugin-data"


@dataclass
class LoadedPlugin:
    """A plugin that has been successfully loaded into the running app.

    Tracks both the plugin instance and the directory it was loaded
    from, so the runtime PluginManagerService can attribute it to a
    source bucket (std/local/installed) and find it for uninstall.
    """

    plugin: Plugin
    install_path: Path
    registered_services: list[str] = field(default_factory=list)


class Gilbert:
    """Main application. Boots the system, loads plugins, starts services."""

    def __init__(self, config: GilbertConfig) -> None:
        self.config = config
        self.registry = ServiceRegistry()
        self.service_manager = ServiceManager()
        self._plugins: list[LoadedPlugin] = []
        # Set to True when a service (typically the plugin manager) has
        # asked the host to exit with the restart-requested exit code so
        # ``gilbert.sh``'s supervisor loop re-runs ``uv sync`` and
        # relaunches Gilbert. Checked by ``__main__.py`` after the web
        # server stops.
        self._restart_requested: bool = False
        # Wired by ``__main__.py`` to flip uvicorn's ``should_exit`` so
        # ``request_restart()`` can actually initiate a graceful
        # shutdown. Without the callback, ``request_restart()`` still
        # sets the flag — the next clean stop will still exit 75 — but
        # nothing triggers the shutdown itself.
        self._shutdown_callback: Callable[[], None] | None = None

    @classmethod
    def create(cls, config_path: str | Path | None = None) -> "Gilbert":
        """Create a Gilbert instance with full config layering including plugin defaults.

        This is the preferred entry point.  It scans plugin directories declared
        in the base config (before user overrides) so that plugin default
        configs participate in the merge chain:

            gilbert.yaml -> plugin defaults -> .gilbert/config.yaml
        """
        from gilbert.config import load_config

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if config_path is not None:
            config = load_config(path=config_path)
            return cls(config)

        # Load base config (without user overrides) to discover plugin directories
        base: dict[str, Any] = {}
        if DEFAULT_CONFIG_PATH.exists():
            base = _load_yaml(DEFAULT_CONFIG_PATH)

        # Also peek at user overrides for additional plugin directories
        overrides: dict[str, Any] = {}
        if OVERRIDE_CONFIG_PATH.exists():
            overrides = _load_yaml(OVERRIDE_CONFIG_PATH)

        # Merge to get the full plugin directory list
        merged_for_dirs = _deep_merge(base, overrides)
        plugins_raw = merged_for_dirs.get("plugins", {})
        if isinstance(plugins_raw, dict):
            directories = plugins_raw.get("directories", [])
        else:
            directories = []

        # Scan plugin directories for manifests
        cache_dir = plugins_raw.get("cache_dir", ".gilbert/plugin-cache") if isinstance(plugins_raw, dict) else ".gilbert/plugin-cache"
        loader = PluginLoader(cache_dir=cache_dir)
        manifests = loader.scan_directories(directories)
        plugin_defaults = loader.collect_default_configs(manifests)

        # Load config with plugin defaults in the merge chain
        config = load_config(plugin_defaults=plugin_defaults)

        instance = cls(config)
        instance._discovered_manifests = manifests
        return instance

    async def start(self) -> None:
        """Initialize all subsystems and start the application."""
        # 1. Logging (first — everything else should be able to log)
        setup_logging(
            level=self.config.logging.level,
            log_file=self.config.logging.file,
            ai_log_file=self.config.logging.ai_log_file,
            loggers=self.config.logging.loggers,
        )
        logger.info("Starting Gilbert...")

        # 2. Register core infrastructure services
        storage = await self._init_storage()

        self.service_manager.register(StorageService(storage))

        event_bus = InMemoryEventBus()
        self.service_manager.register(EventBusService(event_bus))
        self.service_manager.set_event_bus(event_bus)

        # 3. ConfigurationService (early — other services read config from it)
        config_svc = ConfigurationService(self.config)
        self.service_manager.register(config_svc)

        # 3b. Seed entity storage from YAML on first run, then load config
        await config_svc.seed_storage(storage)
        await config_svc.load_from_storage(storage)
        self.config = config_svc.config


        # 4b. Access control (early — other services declare required_role)
        from gilbert.core.services.access_control import AccessControlService

        self.service_manager.register(AccessControlService())

        # 5. User service (always — users are foundational)
        root_hash = self._hash_root_password(self.config.auth.root_password)
        self.service_manager.register(
            UserService(
                root_password_hash=root_hash,
                default_roles=self.config.auth.default_roles,
                allow_user_creation=self.config.auth.allow_user_creation,
            )
        )

        # 6b. Scheduler service (always — timers, alarms, periodic jobs)
        from gilbert.core.services.scheduler import SchedulerService

        self.service_manager.register(SchedulerService())

        # 6c. OCR service
        from gilbert.core.services.ocr import OCRService

        self.service_manager.register(OCRService())

        # 6d. Vision service
        from gilbert.core.services.vision import VisionService

        self.service_manager.register(VisionService())

        # 6e. Tunnel service (before auth, as Google OAuth uses it)
        from gilbert.core.services.tunnel import TunnelService

        self.service_manager.register(TunnelService())

        # 7. Authentication (always enabled, backends created internally)
        self.service_manager.register(AuthService(self.config.auth))

        # 8. Register all optional services (they self-manage enabled/disabled)
        self.service_manager.register(TTSService())
        self.service_manager.register(SpeakerService())
        self.service_manager.register(MusicService())

        from gilbert.core.services.audio_output import AudioOutputService

        self.service_manager.register(AudioOutputService())

        from gilbert.core.services.knowledge import KnowledgeService

        self.service_manager.register(KnowledgeService())

        from gilbert.core.services.presence import PresenceService

        self.service_manager.register(PresenceService())

        from gilbert.core.services.doorbell import DoorbellService

        self.service_manager.register(DoorbellService())

        from gilbert.core.services.screens import ScreenService

        self.service_manager.register(ScreenService())

        from gilbert.core.services.greeting import GreetingService

        self.service_manager.register(GreetingService())

        from gilbert.core.services.backup import BackupService

        self.service_manager.register(BackupService())

        from gilbert.core.services.radio_dj import RadioDJService

        self.service_manager.register(RadioDJService())

        from gilbert.core.services.roast import RoastService

        self.service_manager.register(RoastService())

        self.service_manager.register(InboxService())

        from gilbert.core.services.inbox_ai_chat import InboxAIChatService

        self.service_manager.register(InboxAIChatService())

        # Web search service
        from gilbert.core.services.websearch import WebSearchService

        self.service_manager.register(WebSearchService())

        # Skills service
        from gilbert.core.services.skills import SkillService

        self.service_manager.register(SkillService())

        # Web API service (always — dashboard, system inspector, entity browser)
        from gilbert.core.services.web_api import WebApiService

        self.service_manager.register(WebApiService())

        # Plugin manager — runtime install/uninstall of plugins
        from gilbert.core.services.plugin_manager import PluginManagerService

        plugin_mgr = PluginManagerService()
        plugin_mgr.bind_gilbert(self)
        self.service_manager.register(plugin_mgr)

        self.service_manager.register(AIService())

        # 8. Register factories for hot-swap support
        config_svc.register_factory("tts", self._factory_tts)
        config_svc.register_factory("ai", self._factory_ai)
        config_svc.register_factory("speaker", self._factory_speaker)
        config_svc.register_factory("music", self._factory_music)
        config_svc.register_factory("presence", self._factory_presence)

        # 9. Also register in old registry for backward compat
        self.registry.register(StorageBackend, storage)
        self.registry.register(EventBus, event_bus)
        self.registry.register(ServiceManager, self.service_manager)

        # 10. Load plugins
        await self._load_plugins()

        # 11. Start all services (dependency resolution happens here)
        await self.service_manager.start_all()

        # 12. Let the plugin manager reconcile any runtime-installed
        # plugins that were deferred with ``needs_restart=True`` — if
        # they successfully loaded on this boot (deps are now in the
        # venv), clear the flag so the UI stops nagging about restart.
        plugin_mgr_svc = self.service_manager.list_services().get("plugin_manager")
        if plugin_mgr_svc is not None and hasattr(
            plugin_mgr_svc, "reconcile_loaded_plugins",
        ):
            try:
                await plugin_mgr_svc.reconcile_loaded_plugins(self)
            except Exception:
                logger.exception("Plugin manager reconciliation failed")

        started = len(self.service_manager.started_services)
        failed = len(self.service_manager.failed_services)
        logger.info(
            "Gilbert started — %d services (%d failed), %d plugins",
            started,
            failed,
            len(self._plugins),
        )

    def make_plugin_context(self, name: str) -> PluginContext:
        """Build a ``PluginContext`` for a plugin by name.

        Used both by the boot-time loader and the runtime
        ``PluginManagerService`` so that plugins installed at runtime
        receive the same kind of context (data dir, namespaced storage,
        config slot) as boot-loaded ones.
        """
        plugin_config = self.config.plugins.config

        # Get the raw storage backend for creating namespaced wrappers.
        # Use list_services() since this may run before start_all().
        storage_svc = self.service_manager.list_services().get("storage")
        raw_backend = (
            storage_svc.raw_backend
            if storage_svc is not None and isinstance(storage_svc, StorageProvider)
            else None
        )

        data_dir = PLUGIN_DATA_DIR / name
        data_dir.mkdir(parents=True, exist_ok=True)

        plugin_storage = None
        if raw_backend is not None:
            from gilbert.interfaces.storage import NamespacedStorageBackend

            plugin_storage = NamespacedStorageBackend(
                raw_backend, f"gilbert.plugin.{name}",
            )

        return PluginContext(
            services=self.service_manager,
            config=plugin_config.get(name, {}),
            data_dir=data_dir,
            storage=plugin_storage,
        )

    def list_loaded_plugins(self) -> list[LoadedPlugin]:
        """Return all plugins currently loaded into the app (boot-time + runtime)."""
        return list(self._plugins)

    def find_loaded_plugin(self, name: str) -> LoadedPlugin | None:
        """Look up a loaded plugin by name."""
        for entry in self._plugins:
            if entry.plugin.metadata().name == name:
                return entry
        return None

    def add_loaded_plugin(self, entry: LoadedPlugin) -> None:
        """Record a plugin loaded at runtime so the manager can track it."""
        self._plugins.append(entry)

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback that triggers a graceful host shutdown.

        Called once from ``__main__.py`` with a closure that flips
        uvicorn's ``should_exit`` flag. ``request_restart`` invokes this
        to actually stop the server.
        """
        self._shutdown_callback = callback

    def request_restart(self) -> None:
        """Ask the host process to exit with the restart-requested exit code.

        Sets ``restart_requested`` so ``__main__.py`` can pick the right
        exit code, then calls the shutdown callback (wired at boot) to
        actually initiate a graceful stop. The ``gilbert.sh`` supervisor
        loop catches the exit code, re-runs ``uv sync`` (so any newly
        installed plugin's deps land in the venv), and relaunches
        Gilbert. Safe to call more than once — the second call is a
        no-op.
        """
        if self._restart_requested:
            return
        self._restart_requested = True
        logger.info("Restart requested — initiating graceful shutdown")
        if self._shutdown_callback is not None:
            try:
                self._shutdown_callback()
            except Exception:
                logger.exception("Shutdown callback raised during request_restart")

    @property
    def restart_requested(self) -> bool:
        """Whether a service has asked the host to exit-and-restart."""
        return self._restart_requested

    def remove_loaded_plugin(self, name: str) -> LoadedPlugin | None:
        """Drop a runtime-installed plugin from the loaded list."""
        for i, entry in enumerate(self._plugins):
            if entry.plugin.metadata().name == name:
                return self._plugins.pop(i)
        return None

    async def _load_plugins(self) -> None:
        """Load plugins from discovered manifests and explicit sources."""
        loader = PluginLoader(cache_dir=self.config.plugins.cache_dir)

        # Phase 1: Load plugins from scanned directories (already discovered)
        manifests: list[PluginManifest] = getattr(self, "_discovered_manifests", [])
        sorted_manifests = loader.topological_sort(manifests)

        for manifest in sorted_manifests:
            try:
                # Snapshot the registered services so we can attribute
                # any new ones to this plugin (used later for uninstall).
                before = set(self.service_manager.list_services().keys())
                plugin = loader.load_from_manifest(manifest)
                context = self.make_plugin_context(manifest.name)
                await plugin.setup(context)
                after = set(self.service_manager.list_services().keys())
                self._plugins.append(LoadedPlugin(
                    plugin=plugin,
                    install_path=manifest.path,
                    registered_services=sorted(after - before),
                ))
            except Exception:
                logger.exception("Failed to load plugin: %s", manifest.name)

        # Phase 2: Load explicit sources (legacy path/URL plugins)
        for source in self.config.plugins.sources:
            if not source.enabled:
                continue
            try:
                before = set(self.service_manager.list_services().keys())
                plugin = await loader.load(source.source)
                meta = plugin.metadata()
                context = self.make_plugin_context(meta.name)
                await plugin.setup(context)
                after = set(self.service_manager.list_services().keys())
                self._plugins.append(LoadedPlugin(
                    plugin=plugin,
                    install_path=Path(source.source),
                    registered_services=sorted(after - before),
                ))
            except Exception:
                logger.exception("Failed to load plugin: %s", source.source)

    async def stop(self) -> None:
        """Shut down all subsystems."""
        logger.info("Stopping Gilbert...")

        # Tear down plugins
        for entry in reversed(self._plugins):
            try:
                await entry.plugin.teardown()
            except Exception:
                logger.exception(
                    "Error tearing down plugin: %s",
                    entry.plugin.metadata().name,
                )

        # Stop all services (reverse order, includes storage close)
        await self.service_manager.stop_all()

        logger.info("Gilbert stopped")

    # --- Helpers ---

    @staticmethod
    def _hash_root_password(password: str) -> str:
        """Hash the root password from config. Returns empty string if unset."""
        if not password:
            return ""
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)

    # --- Service factories (for hot-swap via ConfigurationService) ---

    def _factory_ai(self, config: dict[str, Any]) -> Service:
        """Create an AIService from a config section."""
        return AIService()

    def _factory_tts(self, config: dict[str, Any]) -> Service:
        """Create a TTSService from a config section."""
        return TTSService()

    def _factory_speaker(self, config: dict[str, Any]) -> Service:
        """Create a SpeakerService from a config section."""
        return SpeakerService()

    def _factory_music(self, config: dict[str, Any]) -> Service:
        """Create a MusicService from a config section."""
        return MusicService()

    def _factory_presence(self, config: dict[str, Any]) -> Service:
        """Create a PresenceService from a config section."""
        from gilbert.core.services.presence import PresenceService

        return PresenceService()

    # --- Storage init ---

    async def _init_storage(self) -> StorageBackend:
        """Initialize the storage backend based on config."""
        if self.config.storage.backend == "sqlite":
            from pathlib import Path

            db_path = Path(self.config.storage.connection).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            storage = SQLiteStorage(str(db_path))
            await storage.initialize()
            return storage
        raise ValueError(f"Unknown storage backend: {self.config.storage.backend}")
