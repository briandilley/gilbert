"""Tests for InboxRulesService — free filter, match call, dedupe, action, hours."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.core.services import inbox_rules as mod
from gilbert.core.services.inbox_rules import (
    InboxRulesService,
    in_business_hours,
    parse_match_reply,
    rule_passes_filter,
)
from gilbert.interfaces.ai import AIResponse, ChatTurnResult, Message, MessageRole
from gilbert.interfaces.inbox_rules import (
    INBOX_RULE_RUNS_COLLECTION,
    InboxRule,
    RunStatus,
)
from gilbert.storage.sqlite import SQLiteStorage

# A Wednesday, 10:00 in Los Angeles (17:00 UTC).
_OPEN = datetime(2026, 10, 14, 17, 0, tzinfo=UTC)
# The same Wednesday, 22:00 in Los Angeles.
_CLOSED = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)


# ── Fakes ──────────────────────────────────────────────────────


class FakeAI:
    """Satisfies AISamplingProvider and AIProvider by method name."""

    def __init__(self) -> None:
        self.match_reply = '{"match": true, "dedupe_key": "amz:1:delivered", "reason": "delivered"}'
        self.action_reply = "The brakes for the Porsche were delivered."
        self.match_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_error: Exception | None = None

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(self, **kwargs: Any) -> AIResponse:
        self.match_calls.append(kwargs)
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=self.match_reply),
            model="fake",
        )

    async def chat(self, user_message: str, **kwargs: Any) -> ChatTurnResult:
        self.chat_calls.append({"user_message": user_message, **kwargs})
        if self.chat_error is not None:
            raise self.chat_error
        return ChatTurnResult(
            response_text=self.action_reply,
            conversation_id="conv_1",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
        )


class FakeInbox:
    """Satisfies InboxProvider by method name."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}

    async def schedule_send(self, *a: Any, **k: Any) -> Any: ...
    async def cancel_outbox(self, *a: Any, **k: Any) -> bool:
        return False
    async def list_outbox(self, *a: Any, **k: Any) -> list[Any]:
        return []
    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        return self.messages.get(message_id)
    async def get_thread(self, *a: Any, **k: Any) -> list[Any]:
        return []
    async def search_messages(self, *a: Any, **k: Any) -> list[Any]:
        return []
    async def get_mailbox(self, *a: Any, **k: Any) -> Any:
        return None
    async def list_accessible_mailboxes(self, *a: Any, **k: Any) -> list[Any]:
        return []


class FakeSpeaker:
    """Satisfies SpeakerProvider by method name."""

    def __init__(self) -> None:
        self.announced: list[dict[str, Any]] = []

    @property
    def backends(self) -> dict[str, Any]:
        return {}

    def get_backend(self, name: str) -> Any:
        return None

    async def resolve_names(self, names: list[str]) -> dict[str, str]:
        return {}

    async def announce(self, text: str, speaker_names: list[str] | None = None,
                       volume: int | None = None, context: str = "") -> str:
        self.announced.append({"text": text, "speakers": speaker_names})
        return "ok"


class FakeResolver:
    def __init__(self, caps: dict[str, Any]) -> None:
        self.caps = caps

    def get_capability(self, name: str) -> Any:
        return self.caps.get(name)

    def require_capability(self, name: str) -> Any:
        return self.caps[name]

    def get_all(self, name: str) -> list[Any]:
        return [self.caps[name]] if name in self.caps else []


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    now = {"t": _OPEN}
    monkeypatch.setattr(mod, "_now", lambda: now["t"])
    return now


@pytest.fixture
def parts(sqlite_storage: SQLiteStorage) -> dict[str, Any]:
    storage = sqlite_storage
    ai, inbox, speaker = FakeAI(), FakeInbox(), FakeSpeaker()
    svc = InboxRulesService()
    svc._storage = storage
    svc._resolver = FakeResolver(
        {"ai_chat": ai, "inbox": inbox, "speaker_control": speaker}
    )
    return {"svc": svc, "ai": ai, "inbox": inbox, "speaker": speaker, "storage": storage}


def _rule(**kw: Any) -> InboxRule:
    base: dict[str, Any] = {
        "id": "",
        "name": "Deliveries",
        "subject_contains": ["delivered"],
        "match_condition": "The email says a package was delivered.",
        "action_instructions": "Say what was delivered.",
        "action_tools": ["current_expense_match"],
    }
    base.update(kw)
    return InboxRule(**base)


def _event(message_id: str = "m1", subject: str = "Delivered: brake pads") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "mailbox_id": "mbx",
        "subject": subject,
        "sender_email": "vendors@example.com",
        "sender_name": "'Amazon.com' via Vendors",
        "is_inbound": True,
    }


def _store_message(inbox: FakeInbox, message_id: str, at: datetime) -> None:
    inbox.messages[message_id] = {
        "subject": "Delivered: brake pads",
        "sender_email": "vendors@example.com",
        "sender_name": "Amazon",
        "body_text": "Your package with brake pads was delivered. Order 113-1",
        "date": at.isoformat(),
    }


async def _run(parts: dict[str, Any], run_id: str) -> dict[str, Any]:
    row = await parts["storage"].get(INBOX_RULE_RUNS_COLLECTION, run_id)
    assert row is not None
    return dict(row)


# ── Pure helpers ───────────────────────────────────────────────


def test_filter_requires_a_free_filter() -> None:
    rule = _rule(subject_contains=[], sender_contains=[])
    assert not rule_passes_filter(rule, _event())


def test_filter_matches_sender_display_name_and_subject() -> None:
    rule = _rule(sender_contains=["amazon"], subject_contains=["DELIVERED"])
    assert rule_passes_filter(rule, _event())
    assert not rule_passes_filter(rule, _event(subject="Shipped: brake pads"))


def test_filter_respects_mailbox_and_enabled() -> None:
    assert not rule_passes_filter(_rule(mailbox_ids=["other"]), _event())
    assert not rule_passes_filter(_rule(enabled=False), _event())


def test_parse_match_reply() -> None:
    assert parse_match_reply('x {"match": true, "dedupe_key": "A:1", "reason": "r"} y') == (
        True, "a:1", "r")
    assert parse_match_reply("no json")[0] is False
    assert parse_match_reply('{"match": "yes"}')[0] is False


def test_business_hours_uses_rule_timezone_and_days() -> None:
    rule = _rule()
    assert in_business_hours(rule, _OPEN)
    assert not in_business_hours(rule, _CLOSED)
    saturday = datetime(2026, 10, 17, 17, 0, tzinfo=UTC)
    assert not in_business_hours(rule, saturday)


# ── Service flow ───────────────────────────────────────────────


async def test_save_rule_rejects_rule_without_free_filter(parts: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await parts["svc"].save_rule(_rule(subject_contains=[]))


async def test_match_announces_during_business_hours(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule())
    _store_message(parts["inbox"], "m1", _OPEN - timedelta(minutes=5))

    await parts["svc"].process_message(_event())

    assert parts["speaker"].announced == [
        {"text": "The brakes for the Porsche were delivered.", "speakers": None}
    ]
    chat = parts["ai"].chat_calls[0]
    assert chat["tool_filter"] == ("include", ["current_expense_match"])
    assert chat["headless"] is True
    assert parts["ai"].match_calls[0]["tools_override"] == []
    run = await _run(parts, f"{rule.id}:m1")
    assert run["status"] == RunStatus.DONE
    assert run["conversation_id"] == "conv_1"


async def test_filtered_out_email_costs_nothing(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    await parts["svc"].save_rule(_rule())
    await parts["svc"].process_message(_event(subject="Your weekly newsletter"))
    assert parts["ai"].match_calls == []
    assert await parts["svc"].list_runs() == []


async def test_no_match_is_logged_without_action(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule())
    parts["ai"].match_reply = '{"match": false, "dedupe_key": "", "reason": "shipped notice"}'
    _store_message(parts["inbox"], "m1", _OPEN)
    await parts["svc"].process_message(_event())
    run = await _run(parts, f"{rule.id}:m1")
    assert run["status"] == RunStatus.NO_MATCH
    assert run["reason"] == "shipped notice"
    assert parts["ai"].chat_calls == []


async def test_same_dedupe_key_acts_once(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule())
    for mid in ("m1", "m2"):
        _store_message(parts["inbox"], mid, _OPEN)
        await parts["svc"].process_message(_event(message_id=mid))
    assert len(parts["speaker"].announced) == 1
    assert (await _run(parts, f"{rule.id}:m2"))["status"] == RunStatus.DUPLICATE


async def test_same_message_is_evaluated_once(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    await parts["svc"].save_rule(_rule())
    _store_message(parts["inbox"], "m1", _OPEN)
    await parts["svc"].process_message(_event())
    await parts["svc"].process_message(_event())
    assert len(parts["ai"].match_calls) == 1


async def test_off_hours_match_is_held_then_announced(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule(max_age_hours=24))
    clock["t"] = _CLOSED
    _store_message(parts["inbox"], "m1", _CLOSED)
    await parts["svc"].process_message(_event())
    assert (await _run(parts, f"{rule.id}:m1"))["status"] == RunStatus.HELD
    assert parts["ai"].chat_calls == []

    clock["t"] = _CLOSED + timedelta(hours=11)  # 09:00 Thursday in LA
    await parts["svc"].flush_pending()
    assert (await _run(parts, f"{rule.id}:m1"))["status"] == RunStatus.DONE
    assert len(parts["speaker"].announced) == 1


async def test_held_match_goes_stale(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule())  # max_age_hours = 4
    clock["t"] = _CLOSED
    _store_message(parts["inbox"], "m1", _CLOSED)
    await parts["svc"].process_message(_event())
    clock["t"] = _CLOSED + timedelta(hours=11)
    await parts["svc"].flush_pending()
    assert (await _run(parts, f"{rule.id}:m1"))["status"] == RunStatus.STALE
    assert parts["speaker"].announced == []


async def test_dry_run_logs_text_without_speaking(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule(dry_run=True))
    _store_message(parts["inbox"], "m1", _OPEN)
    await parts["svc"].process_message(_event())
    run = await _run(parts, f"{rule.id}:m1")
    assert run["status"] == RunStatus.DRY_RUN
    assert run["output"] == "The brakes for the Porsche were delivered."
    assert parts["speaker"].announced == []


async def test_skip_reply_says_nothing(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule())
    parts["ai"].action_reply = "SKIP"
    _store_message(parts["inbox"], "m1", _OPEN)
    await parts["svc"].process_message(_event())
    assert (await _run(parts, f"{rule.id}:m1"))["status"] == RunStatus.SILENT
    assert parts["speaker"].announced == []


async def test_failed_action_retries_then_fails(
    parts: dict[str, Any], clock: dict[str, datetime]
) -> None:
    rule = await parts["svc"].save_rule(_rule())
    parts["ai"].chat_error = RuntimeError("backend down")
    _store_message(parts["inbox"], "m1", _OPEN)
    await parts["svc"].process_message(_event())
    assert (await _run(parts, f"{rule.id}:m1"))["status"] == RunStatus.RETRY
    await parts["svc"].flush_pending()
    await parts["svc"].flush_pending()
    run = await _run(parts, f"{rule.id}:m1")
    assert run["status"] == RunStatus.FAILED
    assert run["attempts"] == 3
    assert "backend down" in run["error"]
