"""Proposals interface — autonomous self-improvement proposals.

The ``ProposalsProvider`` capability is exposed by the ``ProposalsService``
so other services and the web layer can read proposals without depending
on the concrete service. Records are intentionally plain ``dict[str, Any]``
since they're stored in the entity store and shipped over the WS API.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# ── Constants ────────────────────────────────────────────────────────

PROPOSALS_COLLECTION = "proposals"
"""Entity collection name for stored proposals."""

# Proposal lifecycle status values. ``proposed`` is the initial state
# every autonomously-generated record lands in; the rest are admin-set.
STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_IMPLEMENTED = "implemented"
STATUS_ARCHIVED = "archived"

PROPOSAL_STATUSES: tuple[str, ...] = (
    STATUS_PROPOSED,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_IMPLEMENTED,
    STATUS_ARCHIVED,
)

# The reflection AI is asked to classify proposals into one of these
# kinds so the UI can group them and the implementation prompt can pick
# the right scaffolding (plugin vs. core service vs. config tweak).
KIND_NEW_PLUGIN = "new_plugin"
KIND_MODIFY_PLUGIN = "modify_plugin"
KIND_REMOVE_PLUGIN = "remove_plugin"
KIND_NEW_SERVICE = "new_service"
KIND_REMOVE_SERVICE = "remove_service"
KIND_CONFIG_CHANGE = "config_change"

PROPOSAL_KINDS: tuple[str, ...] = (
    KIND_NEW_PLUGIN,
    KIND_MODIFY_PLUGIN,
    KIND_REMOVE_PLUGIN,
    KIND_NEW_SERVICE,
    KIND_REMOVE_SERVICE,
    KIND_CONFIG_CHANGE,
)


# ── Protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class ProposalsProvider(Protocol):
    """Read access to autonomously-generated improvement proposals.

    Other services can resolve this via ``get_capability("proposals")``
    to surface proposal counts on a dashboard, react to new proposals,
    etc., without importing the concrete ``ProposalsService``.
    """

    async def list_proposals(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return proposal records, newest first."""
        ...

    async def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Fetch a single proposal record by ID."""
        ...

    async def trigger_reflection(self) -> int:
        """Run a reflection cycle now. Returns the number of new proposals created."""
        ...
