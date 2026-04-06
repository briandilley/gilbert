"""Application bootstrap — wires everything together and manages lifecycle."""

import logging
from typing import Any

from gilbert.config import GilbertConfig
from gilbert.core.events import InMemoryEventBus
from gilbert.core.logging import setup_logging
from gilbert.core.registry import ServiceRegistry
from gilbert.core.service_manager import ServiceManager
from gilbert.core.services import (
    AuthService,
    EventBusService,
    MusicService,
    PersonaService,
    SpeakerService,
    StorageService,
    TTSService,
    UserService,
)
from gilbert.core.services.ai import AIService
from gilbert.core.services.configuration import ConfigurationService
from gilbert.core.services.credentials import CredentialService
from gilbert.interfaces.ai import AIBackend
from gilbert.interfaces.events import EventBus
from gilbert.interfaces.music import MusicBackend
from gilbert.interfaces.presence import PresenceBackend
from gilbert.interfaces.plugin import Plugin
from gilbert.interfaces.service import Service
from gilbert.interfaces.speaker import SpeakerBackend
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tts import TTSBackend
from gilbert.plugins.loader import PluginLoader
from gilbert.storage.sqlite import SQLiteStorage

logger = logging.getLogger(__name__)


class Gilbert:
    """Main application. Boots the system, loads plugins, starts services."""

    def __init__(self, config: GilbertConfig) -> None:
        self.config = config
        self.registry = ServiceRegistry()
        self.service_manager = ServiceManager()
        self._plugins: list[Plugin] = []

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

        # 4. CredentialService
        self.service_manager.register(CredentialService(self.config.credentials))

        # 4b. Access control (early — other services declare required_role)
        from gilbert.core.services.access_control import AccessControlService

        self.service_manager.register(AccessControlService())

        # 5. User service (always — users are foundational)
        root_hash = self._hash_root_password(self.config.auth.root_password)
        self.service_manager.register(
            UserService(
                root_password_hash=root_hash,
                default_roles=self.config.auth.default_roles,
            )
        )

        # 6. Persona service (always — AI persona is core)
        self.service_manager.register(PersonaService())

        # 6b. Scheduler service (always — timers, alarms, periodic jobs)
        from gilbert.core.services.scheduler import SchedulerService

        self.service_manager.register(SchedulerService())

        # 6c. OCR service (always — gracefully degrades if tesseract not installed)
        from gilbert.core.services.ocr import OCRService

        self.service_manager.register(OCRService())

        # 6d. Vision service (if knowledge + vision enabled)
        if self.config.knowledge.enabled and self.config.knowledge.vision_enabled:
            from gilbert.core.services.vision import VisionService

            self.service_manager.register(VisionService())

        # 6. Tunnel service (if enabled — before auth, as Google OAuth uses it)
        if self.config.tunnel.enabled:
            from gilbert.core.services.tunnel import TunnelService

            self.service_manager.register(
                TunnelService(self.config.tunnel, self.config.web.port)
            )

        # 7. Google API service (if enabled — before auth, as auth may need it)
        if self.config.google.enabled:
            from gilbert.core.services.google import GoogleService

            self.service_manager.register(GoogleService(self.config.google))

            # Register Google Directory as a user provider if "directory" account exists.
            if "directory" in self.config.google.accounts:
                from gilbert.integrations.google_directory import GoogleDirectoryService

                domain = ""
                for prov in self.config.auth.providers:
                    if prov.type == "google" and prov.domain:
                        domain = prov.domain
                        break
                self.service_manager.register(
                    GoogleDirectoryService(account="directory", domain=domain)
                )

        # 8. Authentication providers
        if self.config.auth.enabled:
            self.service_manager.register(AuthService(self.config.auth))

            for prov_cfg in self.config.auth.providers:
                if not prov_cfg.enabled:
                    continue
                if prov_cfg.type == "local":
                    from gilbert.integrations.local_auth import (
                        LocalAuthenticationService,
                    )

                    self.service_manager.register(LocalAuthenticationService())
                elif prov_cfg.type == "google":
                    from gilbert.integrations.google_auth import (
                        GoogleAuthenticationService,
                    )

                    self.service_manager.register(
                        GoogleAuthenticationService(
                            domain=prov_cfg.domain,
                            use_tunnel=prov_cfg.settings.get("use_tunnel", True),
                        )
                    )

        # 9. Register optional services (structural deps via constructor)
        if self.config.tts.enabled:
            tts_backend = self._create_tts_backend(self.config.tts.backend)
            self.service_manager.register(
                TTSService(tts_backend, self.config.tts.credential)
            )

        if self.config.speaker.enabled:
            speaker_backend = self._create_speaker_backend(self.config.speaker.backend)
            self.service_manager.register(SpeakerService(speaker_backend))

        if self.config.music.enabled:
            music_backend = self._create_music_backend(self.config.music.backend)
            self.service_manager.register(
                MusicService(music_backend, self.config.music.credential)
            )

        if self.config.knowledge.enabled:
            from gilbert.core.services.knowledge import KnowledgeService

            has_gdrive = any(s.type == "gdrive" for s in self.config.knowledge.sources if s.enabled)
            self.service_manager.register(KnowledgeService(has_gdrive=has_gdrive))

        if self.config.presence.enabled:
            from gilbert.core.services.presence import PresenceService

            presence_backend = self._create_presence_backend(self.config.presence.backend)
            self.service_manager.register(PresenceService(presence_backend))

        if self.config.doorbell.enabled:
            from gilbert.core.services.doorbell import DoorbellService

            self.service_manager.register(DoorbellService())

        if self.config.screens.enabled:
            from gilbert.core.services.screens import ScreenService

            self.service_manager.register(ScreenService())

        if self.config.greeting.enabled:
            from gilbert.core.services.greeting import GreetingService

            self.service_manager.register(GreetingService())

        # Memory service (always — uses entity storage)
        from gilbert.core.services.memory import MemoryService

        self.service_manager.register(MemoryService())

        if self.config.ai.enabled:
            ai_backend = self._create_ai_backend(self.config.ai.backend)
            self.service_manager.register(
                AIService(ai_backend, self.config.ai.credential)
            )

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

        # 10. Load plugins (they can register more services)
        loader = PluginLoader()
        for source in self.config.plugins:
            if source.enabled:
                try:
                    plugin = await loader.load(source.source)
                    await plugin.setup(self.service_manager)
                    self._plugins.append(plugin)
                except Exception:
                    logger.exception("Failed to load plugin: %s", source.source)

        # 11. Start all services (dependency resolution happens here)
        await self.service_manager.start_all()

        started = len(self.service_manager.started_services)
        failed = len(self.service_manager.failed_services)
        logger.info(
            "Gilbert started — %d services (%d failed), %d plugins",
            started,
            failed,
            len(self._plugins),
        )

    async def stop(self) -> None:
        """Shut down all subsystems."""
        logger.info("Stopping Gilbert...")

        # Tear down plugins
        for plugin in reversed(self._plugins):
            try:
                await plugin.teardown()
            except Exception:
                logger.exception("Error tearing down plugin: %s", plugin.metadata().name)

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

    # --- Backend factories ---

    @staticmethod
    def _create_ai_backend(backend_name: str) -> AIBackend:
        """Create an AI backend by name."""
        if backend_name == "anthropic":
            from gilbert.integrations.anthropic_ai import AnthropicAI

            return AnthropicAI()
        raise ValueError(f"Unknown AI backend: {backend_name}")

    @staticmethod
    def _create_music_backend(backend_name: str) -> MusicBackend:
        """Create a music backend by name."""
        if backend_name == "spotify":
            from gilbert.integrations.spotify_music import SpotifyMusic

            return SpotifyMusic()
        raise ValueError(f"Unknown music backend: {backend_name}")

    @staticmethod
    def _create_presence_backend(backend_name: str) -> PresenceBackend:
        """Create a presence backend by name."""
        if backend_name == "unifi":
            from gilbert.integrations.unifi import UniFiPresenceBackend

            return UniFiPresenceBackend()
        raise ValueError(f"Unknown presence backend: {backend_name}")

    @staticmethod
    def _create_speaker_backend(backend_name: str) -> SpeakerBackend:
        """Create a speaker backend by name."""
        if backend_name == "sonos":
            from gilbert.integrations.sonos_speaker import SonosSpeaker

            return SonosSpeaker()
        raise ValueError(f"Unknown speaker backend: {backend_name}")

    @staticmethod
    def _create_tts_backend(backend_name: str) -> TTSBackend:
        """Create a TTS backend by name."""
        if backend_name == "elevenlabs":
            from gilbert.integrations.elevenlabs_tts import ElevenLabsTTS

            return ElevenLabsTTS()
        raise ValueError(f"Unknown TTS backend: {backend_name}")

    # --- Service factories (for hot-swap via ConfigurationService) ---

    def _factory_ai(self, config: dict[str, Any]) -> Service:
        """Create an AIService from a config section."""
        backend = self._create_ai_backend(config.get("backend", "anthropic"))
        return AIService(backend=backend, credential_name=config.get("credential", ""))

    def _factory_tts(self, config: dict[str, Any]) -> Service:
        """Create a TTSService from a config section."""
        backend = self._create_tts_backend(config.get("backend", "elevenlabs"))
        return TTSService(backend=backend, credential_name=config.get("credential", ""))

    def _factory_speaker(self, config: dict[str, Any]) -> Service:
        """Create a SpeakerService from a config section."""
        backend = self._create_speaker_backend(config.get("backend", "sonos"))
        return SpeakerService(backend=backend)

    def _factory_music(self, config: dict[str, Any]) -> Service:
        """Create a MusicService from a config section."""
        backend = self._create_music_backend(config.get("backend", "spotify"))
        return MusicService(backend=backend, credential_name=config.get("credential", ""))

    def _factory_presence(self, config: dict[str, Any]) -> Service:
        """Create a PresenceService from a config section."""
        from gilbert.core.services.presence import PresenceService

        backend = self._create_presence_backend(config.get("backend", "unifi"))
        return PresenceService(backend=backend)

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
