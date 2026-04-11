"""Core service wrappers — thin adapters making existing components discoverable."""

from gilbert.core.services.access_control import AccessControlService
from gilbert.core.services.ai import AIService
from gilbert.core.services.auth import AuthService
from gilbert.core.services.doorbell import DoorbellService
from gilbert.core.services.configuration import ConfigurationService
from gilbert.core.services.event_bus import EventBusService
from gilbert.core.services.inbox import InboxService
from gilbert.core.services.inbox_ai_chat import InboxAIChatService
from gilbert.core.services.music import MusicService
from gilbert.core.services.speaker import SpeakerService
from gilbert.core.services.storage import StorageService
from gilbert.core.services.tts import TTSService
from gilbert.core.services.users import UserService

__all__ = [
    "AccessControlService",
    "AIService",
    "AuthService",
    "DoorbellService",
    "ConfigurationService",
    "EventBusService",
    "InboxService",
    "InboxAIChatService",
    "MusicService",
    "SpeakerService",
    "StorageService",
    "TTSService",
    "UserService",
]
