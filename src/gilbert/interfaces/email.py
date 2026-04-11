"""Email backend interface — sync source and send transport.

The backend is only consulted during sync (list IDs, fetch new, mark read)
and when sending. All reads come from entity storage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EmailAddress:
    """An email address with optional display name."""

    email: str
    name: str = ""

    def __str__(self) -> str:
        if self.name:
            return f"{self.name} <{self.email}>"
        return self.email


@dataclass(frozen=True)
class EmailMessage:
    """A parsed email message."""

    message_id: str
    thread_id: str
    subject: str
    sender: EmailAddress
    to: list[EmailAddress]
    cc: list[EmailAddress]
    body_text: str
    date: datetime
    body_html: str = ""
    in_reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailAttachment:
    """A file attachment for an outbound email."""

    filename: str
    data: bytes
    mime_type: str = "application/octet-stream"


class EmailBackend(ABC):
    """Abstract email transport — sync source and outbound sender.

    The backend is only used for:
    1. Listing message IDs (cheap, no content)
    2. Fetching full content for new messages
    3. Marking messages as read after syncing them locally
    4. Sending outbound email

    All reads/searches/state changes after sync go through entity storage.
    """

    _registry: dict[str, type["EmailBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            EmailBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["EmailBackend"]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        """Authenticate and prepare the backend for use."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...

    @abstractmethod
    async def list_message_ids(self, query: str = "", max_results: int = 50) -> list[str]:
        """List IDs of recent messages (cheap — no content fetched)."""
        ...

    @abstractmethod
    async def get_message(self, message_id: str) -> EmailMessage | None:
        """Fetch a single message by ID. Returns None if not found."""
        ...

    @abstractmethod
    async def mark_read(self, message_id: str) -> None:
        """Mark a message as read in the remote provider (called during sync)."""
        ...

    @abstractmethod
    async def send(
        self,
        to: list[EmailAddress],
        subject: str,
        body_html: str,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        in_reply_to: str = "",
        thread_id: str = "",
        attachments: list[EmailAttachment] | None = None,
    ) -> str:
        """Send an email. Returns the sent message's ID."""
        ...
