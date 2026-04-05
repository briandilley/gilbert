"""Core service wrappers — thin adapters making existing components discoverable."""

from gilbert.core.services.ai import AIService
from gilbert.core.services.auth import AuthService
from gilbert.core.services.doorbell import DoorbellService
from gilbert.core.services.configuration import ConfigurationService
from gilbert.core.services.credentials import CredentialService
from gilbert.core.services.event_bus import EventBusService
from gilbert.core.services.music import MusicService
from gilbert.core.services.persona import PersonaService
from gilbert.core.services.speaker import SpeakerService
from gilbert.core.services.storage import StorageService
from gilbert.core.services.tts import TTSService
from gilbert.core.services.users import UserService

__all__ = [
    "AIService",
    "AuthService",
    "DoorbellService",
    "ConfigurationService",
    "CredentialService",
    "EventBusService",
    "MusicService",
    "PersonaService",
    "SpeakerService",
    "StorageService",
    "TTSService",
    "UserService",
]
