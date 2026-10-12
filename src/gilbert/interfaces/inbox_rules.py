"""Inbox rules — shared data shapes.

An *inbox rule* reacts to new inbound email in three stages (ADR-0023):

1. A free filter on mailbox, sender and subject. No AI cost.
2. A small AI match call on the subject and a snippet. It answers
   match / no match and returns a dedupe key.
3. An AI action turn limited to the rule's allowed tools. Its final text
   is announced on the speakers (business-hours gated) unless it is empty
   or ``SKIP``.

The service lives in ``core/services/inbox_rules.py``. This module holds the
record shape and the run statuses so the web layer and plugins (which seed
rules through migrations) share one definition.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

INBOX_RULES_COLLECTION = "inbox_rules"
INBOX_RULE_RUNS_COLLECTION = "inbox_rule_runs"
INBOX_RULE_DEDUPE_COLLECTION = "inbox_rule_dedupe"

# The action turn answers with this token when nothing should be said.
SKIP_TOKEN = "SKIP"


class RunStatus:
    """Status values for one (rule, message) run record."""

    NO_MATCH = "no_match"
    DUPLICATE = "duplicate"
    HELD = "held"
    RETRY = "retry"
    DONE = "done"
    SILENT = "silent"
    DRY_RUN = "dry_run"
    STALE = "stale"
    FAILED = "failed"

    # Runs the flush job picks up again.
    PENDING = frozenset({HELD, RETRY})


@dataclass
class InboxRule:
    """One admin-editable inbox rule."""

    id: str
    name: str
    enabled: bool = True
    dry_run: bool = False

    # Stage 1: free filter. Each list matches case-insensitively as a
    # substring; an empty list matches everything, but a rule must set at
    # least one of ``sender_contains`` / ``subject_contains``.
    mailbox_ids: list[str] = field(default_factory=list)
    sender_contains: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)

    # Stage 2: plain-language condition for the small match call.
    match_condition: str = ""
    match_profile: str = "light"

    # Stage 3: what the action turn does, and the tools it may use.
    action_instructions: str = ""
    action_tools: list[str] = field(default_factory=list)
    action_profile: str = "standard"

    # Output.
    announce: bool = True
    speakers: list[str] = field(default_factory=list)
    active_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    start_hour: int = 8
    end_hour: int = 18
    timezone: str = "America/Los_Angeles"
    max_age_hours: float = 4.0
    dedupe_window_hours: float = 72.0

    created_at: str = ""
    updated_at: str = ""

    def has_free_filter(self) -> bool:
        return any(s.strip() for s in self.sender_contains + self.subject_contains)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InboxRule:
        known = {f for f in cls.__dataclass_fields__}
        values = {k: v for k, v in data.items() if k in known}
        if "id" not in values:
            values["id"] = str(data.get("_id", ""))
        values.setdefault("name", "")
        return cls(**values)


@runtime_checkable
class InboxRulesProvider(Protocol):
    """Capability ``inbox_rules``: list and save inbox rules."""

    async def list_rules(self) -> list[InboxRule]: ...

    async def save_rule(self, rule: InboxRule) -> InboxRule: ...

    async def delete_rule(self, rule_id: str) -> None: ...

    async def list_runs(
        self, rule_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...
