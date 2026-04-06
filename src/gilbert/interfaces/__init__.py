"""Gilbert interfaces — ABCs and protocol definitions."""

from gilbert.interfaces.ai import (
    AIBackend,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    TokenUsage,
)
from gilbert.interfaces.auth import (
    AuthInfo,
    AuthProvider,
    UserContext,
)
from gilbert.interfaces.configuration import (
    ConfigParam,
    Configurable,
)
from gilbert.interfaces.email import (
    EmailAddress,
    EmailAttachment,
    EmailBackend,
    EmailMessage,
)
from gilbert.interfaces.credentials import (
    AnyCredential,
    ApiKeyCredential,
    CredentialType,
    GoogleServiceAccountCredential,
    UsernamePasswordCredential,
)
from gilbert.interfaces.events import Event, EventBus, EventHandler
from gilbert.interfaces.plugin import Plugin, PluginMeta
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
    ToolResult,
)
from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
    TTSBackend,
    Voice,
)
from gilbert.interfaces.users import UserBackend

__all__ = [
    "AuthInfo",
    "AuthProvider",
    "ConfigParam",
    "Configurable",
    "AIBackend",
    "AIRequest",
    "AIResponse",
    "AnyCredential",
    "ApiKeyCredential",
    "AudioFormat",
    "EmailAddress",
    "EmailAttachment",
    "EmailBackend",
    "EmailMessage",
    "CredentialType",
    "Event",
    "EventBus",
    "EventHandler",
    "Filter",
    "FilterOp",
    "GoogleServiceAccountCredential",
    "IndexDefinition",
    "Message",
    "MessageRole",
    "Plugin",
    "PluginMeta",
    "Query",
    "Service",
    "ServiceInfo",
    "ServiceResolver",
    "SortField",
    "StopReason",
    "StorageBackend",
    "SynthesisRequest",
    "SynthesisResult",
    "TTSBackend",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
    "ToolParameter",
    "ToolParameterType",
    "ToolProvider",
    "ToolResult",
    "UserBackend",
    "UserContext",
    "UsernamePasswordCredential",
    "Voice",
]
