"""System-prompt contribution framework.

Lets plugins (or any service) contribute fragments to a target system
prompt without core knowing about them. The owning service of an
extensible prompt (e.g. ``AutonomousAgentService`` for the agent's
system prompt) renders its base template, then concatenates every
``enabled`` fragment whose ``target`` matches.

Each fragment is owned by ONE service. That service is responsible
for storing its body + enabled state — the conventional pattern is
two ``ConfigParam``s on the contributing service:

- ``<key>_prompt_contribution`` — multiline + ``ai_prompt=True``.
  The fragment body. Default is whatever the plugin author thinks
  the agent should know about its plugin.
- ``<key>_prompt_contribution_enabled`` — boolean. Lets the operator
  disable the fragment without losing the body.

The Settings UI surfaces both alongside the contributing service's
other config — and on the *target* prompt (e.g. the agent's
``system_prompt_template``) shows a 'this prompt is extensible' badge
plus a live list of currently-contributing fragments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PromptFragment:
    """One fragment a service contributes to a named prompt target.

    ``target`` is a stable string that the prompt's owning service
    (the consumer of fragments) checks against. Conventional values:

    - ``"agent.system_prompt"`` — appended to AutonomousAgentService's
      per-run system frame.

    Pages may add more targets over time; this dataclass is the
    source of truth.
    """

    fragment_id: str
    """Globally unique. Conventionally ``<plugin>.<purpose>``,
    e.g. ``"browser.bot-blocks"``."""

    target: str
    """Which prompt to contribute to. See class docstring for known
    values."""

    label: str
    """Short human-readable name shown in the target prompt's
    'contributors' list in the Settings UI. Optional but
    recommended."""

    body: str
    """The fragment text. Concatenated to the target prompt with a
    blank line separator. Empty string is treated as 'nothing to
    contribute right now' (e.g. service is disabled or hasn't
    finished initializing) and skipped silently."""

    enabled: bool = True
    """When False, the fragment is still listed in the contributors
    UI (so the operator can toggle it back on) but skipped at
    prompt-render time."""

    description: str = ""
    """Optional one-liner shown alongside the label, e.g.
    'Tells the agent how to handle Cloudflare blocks via VNC'."""


@runtime_checkable
class SystemPromptContributor(Protocol):
    """Service-level capability for contributing prompt fragments.

    Declare ``"system_prompt_contributor"`` in ``ServiceInfo.capabilities``
    so consumers can find you via ``resolver.get_all_by_capability``.
    Contributing services typically also implement ``Configurable``
    so their fragment body + enabled state are editable in Settings.
    """

    def get_prompt_fragments(self) -> list[PromptFragment]:
        """Return every fragment this service contributes.

        Called once per agent run / chat turn (cheap — just reads
        cached config values), so it's fine to do simple computation
        here. Don't do I/O.
        """
        ...
