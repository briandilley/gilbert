"""Configuration interface — config parameter descriptions and the Configurable protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from gilbert.interfaces.tools import ToolParameterType


@dataclass(frozen=True)
class ConfigParam:
    """Describes a single configurable parameter.

    Used by services to declare what they accept, enabling AI introspection,
    runtime configuration changes, and auto-generated web UI forms.
    """

    key: str
    type: ToolParameterType
    description: str
    default: Any = None
    restart_required: bool = False
    sensitive: bool = False
    """Mask value in the UI (for passwords, API keys, etc.)."""
    choices: tuple[str, ...] | None = None
    """Fixed set of allowed values — renders as a dropdown in the UI."""
    multiline: bool = False
    """Render as a multi-line textarea instead of a single-line input."""
    choices_from: str = ""
    """Dynamic choices resolved at runtime (e.g., ``"speakers"`` to list speaker names)."""
    backend_param: bool = False
    """True if this param is declared by a backend, not the service itself."""
    ai_prompt: bool = False
    """True if this field holds an AI prompt. The Settings UI renders an
    "Author with AI" button next to the field that lets the user describe
    a desired change; the backend rewrites the prompt via the AI and the
    UI shows a side-by-side diff before applying."""

    extensible_target: str = ""
    """When non-empty, marks this prompt as a target other services /
    plugins can contribute fragments to via the
    ``SystemPromptContributor`` capability + ``PromptFragment(target=...)``.
    The Settings UI shows a 'plugins can extend this prompt' indicator
    plus a live list of currently-contributing fragments (each with
    its own per-fragment enable / disable toggle, sourced from the
    contributing service's own ConfigParams). Conventional value:
    ``"agent.system_prompt"`` for the autonomous agent's system frame."""


@dataclass(frozen=True)
class ConfigAction:
    """An action button advertised by a service or backend on its settings page.

    Unlike a ``ConfigParam``, which writes a value, an action triggers a
    server-side operation (e.g. "Test connection", "Link account",
    "Re-discover"). Services declare actions via ``ConfigActionProvider``;
    backends declare them via ``backend_actions()`` and the owning service
    forwards invocations.

    The ``key`` is unique within the service's namespace. ``required_role``
    gates who can click the button (defaults to admin, matching the rest of
    the settings page).
    """

    key: str
    label: str
    description: str = ""
    backend_action: bool = False
    """True if declared by a backend; set automatically when the service
    merges backend actions into its own list."""
    backend: str = ""
    """Name of the backend this action belongs to, when declared by a
    backend. The UI uses this to filter visible actions by the currently
    selected backend (dropdown value), so switching backends in an
    unsaved state still shows the right set of buttons without a
    round-trip. Empty for service-level actions."""
    confirm: str = ""
    """Optional confirmation prompt shown before invocation."""
    required_role: str = "admin"
    hidden: bool = False
    """If True, the UI does not render a button for this action in the
    initial action list, but the RPC will still accept invocations for
    its key. Used for two-phase flows: the visible action returns a
    ``followup_action`` that points at the hidden one, so the same
    button re-labels to 'Continue' without the followup key needing to
    exist on the settings page as its own button."""


@dataclass(frozen=True)
class ConfigActionResult:
    """Result of invoking a ``ConfigAction``.

    The UI interprets ``status`` + the optional fields:

    - ``ok`` — show ``message`` as a success toast.
    - ``error`` — show ``message`` as an error toast.
    - ``pending`` — show ``message`` as an info toast; if ``open_url`` is
      set, open it in a new tab; if ``followup_action`` is set, the button
      relabels to "Continue" and the next click invokes that action key.

    ``data`` is free-form JSON-serializable output for actions that want to
    return structured results to the UI beyond the toast text.
    """

    status: Literal["ok", "error", "pending"]
    message: str = ""
    open_url: str = ""
    followup_action: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConfigActionProvider(Protocol):
    """Protocol for services that expose action buttons on their settings page.

    Services implement this in addition to ``Configurable`` when they want
    to advertise one-click operations beyond plain config writes. Backends
    expose actions via ``BackendActionProvider``; the owning service is
    responsible for merging those into its own ``config_actions()`` list
    (with ``backend_action=True``) and forwarding invocations.
    """

    def config_actions(self) -> list[ConfigAction]:
        """Declare action buttons for this service's settings page."""
        ...

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        """Invoke an action by key. ``payload`` is free-form per-action."""
        ...


@runtime_checkable
class BackendActionProvider(Protocol):
    """Protocol for backends that expose action buttons in their settings.

    Backends opt in by implementing both methods — no ABC changes required.
    The owning service's ``config_actions()`` should merge the result of
    ``backend_actions()`` (with ``backend_action=True``) into its own list,
    and forward invocations to ``invoke_backend_action()``.

    ``backend_actions()`` is invoked on an instance, but concrete backends
    typically implement it as a ``@classmethod`` alongside
    ``backend_config_params()`` so the settings UI can list actions before
    the backend is initialized.
    """

    def backend_actions(self) -> list[ConfigAction]:
        """Declare action buttons contributed by this backend."""
        ...

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        """Invoke a backend-level action by key."""
        ...


@runtime_checkable
class Configurable(Protocol):
    """Protocol for services that accept runtime configuration.

    Services implementing this are auto-discovered by ConfigurationService.
    They describe their parameters (for AI introspection) and handle
    runtime config changes.
    """

    @property
    def config_namespace(self) -> str:
        """Config section name this service owns (e.g., 'ai', 'tts')."""
        ...

    @property
    def config_category(self) -> str:
        """UI grouping category (e.g., 'Media', 'Intelligence', 'Security')."""
        ...

    def config_params(self) -> list[ConfigParam]:
        """Describe all configurable parameters."""
        ...

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """Called with the full config section when tunable params change."""
        ...


@runtime_checkable
class ConfigurationReader(Protocol):
    """Protocol for reading and writing configuration values.

    Services resolve this via ``get_capability("configuration")`` to access
    config without depending on the concrete ConfigurationService.
    """

    def get(self, path: str) -> Any:
        """Get a config value by dot-path (e.g., ``'ai.model'``)."""
        ...

    def get_section(self, namespace: str) -> dict[str, Any]:
        """Get the full config section for a namespace."""
        ...

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        """Get a config section, returning ``{}`` if missing."""
        ...

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        """Set a config value and return the updated section."""
        ...
