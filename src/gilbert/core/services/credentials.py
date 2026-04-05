"""Credential service — provides named credentials to other services."""

import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.credentials import AnyCredential, CredentialType
from gilbert.interfaces.service import Service, ServiceInfo
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


class CredentialService(Service):
    """Serves named credentials loaded from configuration."""

    def __init__(self, credentials: dict[str, AnyCredential]) -> None:
        self._credentials = credentials

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="credentials",
            capabilities=frozenset({"credentials"}),
        )

    def get(self, name: str) -> AnyCredential | None:
        """Get a credential by name, or None if not found."""
        return self._credentials.get(name)

    def require(self, name: str) -> AnyCredential:
        """Get a credential by name, or raise if not found."""
        cred = self._credentials.get(name)
        if cred is None:
            raise LookupError(f"Credential not found: {name}")
        return cred

    def get_by_type(self, cred_type: CredentialType) -> dict[str, AnyCredential]:
        """Get all credentials of a given type, keyed by name."""
        return {
            name: cred
            for name, cred in self._credentials.items()
            if cred.type == cred_type
        }

    def list_names(self) -> list[str]:
        """List all credential names."""
        return sorted(self._credentials.keys())

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "credentials"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="*", type=ToolParameterType.OBJECT,
                description="Named credentials (all changes require restart for security).",
                restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        pass  # Credential changes are restart_required
