"""Core service wrappers — thin adapters making existing components discoverable."""

from gilbert.core.services.ai import AIService
from gilbert.core.services.configuration import ConfigurationService
from gilbert.core.services.credentials import CredentialService
from gilbert.core.services.event_bus import EventBusService
from gilbert.core.services.storage import StorageService
from gilbert.core.services.tts import TTSService

__all__ = [
    "AIService",
    "ConfigurationService",
    "CredentialService",
    "EventBusService",
    "StorageService",
    "TTSService",
]
