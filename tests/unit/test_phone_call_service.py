"""Unit tests for ``PhoneCallService`` — service surface + pure logic.

The conversation brain itself drives async loops over real backends
(STT / TTS / LLM / carrier) and is exercised manually + via the staging
deployment. These tests cover the deterministic bits: config wiring,
concurrency cap, persistence shape, tool definitions, and the
intervention queue.
"""

from __future__ import annotations

import pytest

from gilbert.core.services.phone_call import (
    _DEFAULT_CALL_SYSTEM_PROMPT,
    _DEFAULT_OPENING_DISCLOSURE,
    PhoneCallBrainToolProvider,
    PhoneCallService,
    _CallRecord,
    _record_from_dict,
    _summarize_for_list,
)
from gilbert.interfaces.telephony import CallStatus

# ── Service surface ───────────────────────────────────────────────────


def test_service_info_advertises_phone_call_capabilities() -> None:
    """Other services find us via the capability registry — the names
    must stay stable or the AI tool dispatch + WS handler discovery
    break silently."""
    info = PhoneCallService().service_info()
    assert info.name == "phone_call"
    assert "phone_calls" in info.capabilities
    assert "ai_tools" in info.capabilities
    assert "ws_handlers" in info.capabilities
    # Hard requirements — service won't start if these aren't available.
    for required in (
        "entity_storage",
        "event_bus",
        "ai_chat",
        "text_to_speech",
        "speech_to_text",
    ):
        assert required in info.requires


def test_config_namespace_matches_settings_section() -> None:
    """``phone_call`` is what the Settings UI category groups under and
    what plugin tests / migrations key off. Don't change without a
    plan."""
    svc = PhoneCallService()
    assert svc.config_namespace == "phone_call"
    assert svc.config_category == "Phone"


def test_config_params_includes_disclosure_prompt_as_ai_prompt() -> None:
    """The opening-disclosure script must be flagged ``ai_prompt`` so
    the prompt editor recognizes it as an AI-tunable text — otherwise
    operators editing it would hit the regular string-input UI which
    doesn't preserve newlines / quote handling well."""
    svc = PhoneCallService()
    params = {p.key: p for p in svc.config_params()}
    assert "opening_disclosure_prompt" in params
    assert params["opening_disclosure_prompt"].ai_prompt is True
    assert params["opening_disclosure_prompt"].multiline is True
    assert "call_system_prompt" in params
    assert params["call_system_prompt"].ai_prompt is True


def test_get_tools_returns_empty_when_disabled() -> None:
    """A disabled service must not expose tools to the AI — otherwise
    Gilbert would try to invoke ``make_phone_call`` and fail at execute
    time instead of just not seeing the tool at all."""
    svc = PhoneCallService()
    # No start() = not enabled
    assert svc.get_tools() == []


def test_get_tools_returns_make_phone_call_when_enabled() -> None:
    """The tool's name + parameter shape are part of the AI's prompt;
    if they drift, the model's tool calls will start mis-binding."""
    svc = PhoneCallService()
    svc._enabled = True
    tools = svc.get_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "make_phone_call"
    assert t.slash_group == "call"
    assert t.slash_command == "make"
    param_names = {p.name for p in t.parameters}
    assert "to_number" in param_names
    assert "brief" in param_names
    assert "callback_number" in param_names


# ── Brain-tool surface (the in-call LLM-callable tools) ───────────────


def test_brain_tools_includes_lifecycle_tools() -> None:
    """The LLM needs at minimum a way to end the call cleanly and to
    bail out. Without these the call hits the watchdog timeout."""
    by_name = {t.name: t for t in PhoneCallBrainToolProvider().get_brain_tools()}
    for required in (
        "hang_up",
        "confirm_and_end",
        "escalate_to_user",
        "note",
        "send_dtmf",
    ):
        assert required in by_name


def test_brain_tools_confirm_and_end_takes_structured_summary() -> None:
    """``summary`` must be a structured object so the brain can write
    typed outcome fields onto the call record — flattening it to a
    string would lose the ability to query outcomes downstream."""
    confirm = next(
        t
        for t in PhoneCallBrainToolProvider().get_brain_tools()
        if t.name == "confirm_and_end"
    )
    assert any(p.name == "summary" for p in confirm.parameters)


async def test_brain_tools_hang_up_returns_end_conversation() -> None:
    """``hang_up`` must signal END_CONVERSATION so the engine drops
    the line. Regression-guard: the provider's return-value protocol
    is what tells the dispatch loop to stop processing further turns."""
    from gilbert.interfaces.conversation import (
        BrainToolResult,
        ConversationContext,
    )

    provider = PhoneCallBrainToolProvider()
    outcome: dict = {}
    turns: list[tuple[str, str]] = []

    async def _record(who: str, text: str) -> None:
        turns.append((who, text))

    async def _publish(event: str, data: dict) -> None:
        pass

    ctx = ConversationContext(
        session=None,  # type: ignore[arg-type]  — hang_up doesn't touch session
        outcome=outcome,
        record_turn=_record,
        publish_event=_publish,
    )
    result = await provider.handle_brain_tool(
        "hang_up", {"reason": "test"}, ctx
    )
    assert result == BrainToolResult.END_CONVERSATION
    assert outcome["hang_up_reason"] == "test"
    assert any("(brain hung up:" in text for _, text in turns)


async def test_brain_tools_escalate_returns_escalate() -> None:
    """``escalate_to_user`` must end the call AND signal escalation so
    the wrapper can fire its specific bus event downstream."""
    from gilbert.interfaces.conversation import (
        BrainToolResult,
        ConversationContext,
    )

    provider = PhoneCallBrainToolProvider()
    outcome: dict = {}
    published: list[tuple[str, dict]] = []

    async def _record(who: str, text: str) -> None:
        pass

    async def _publish(event: str, data: dict) -> None:
        published.append((event, data))

    ctx = ConversationContext(
        session=None,  # type: ignore[arg-type]
        outcome=outcome,
        record_turn=_record,
        publish_event=_publish,
    )
    result = await provider.handle_brain_tool(
        "escalate_to_user", {"reason": "needs human"}, ctx
    )
    assert result == BrainToolResult.ESCALATE
    assert outcome["escalated"] is True
    assert outcome["escalation_reason"] == "needs human"
    assert any("escalation_requested" in event for event, _ in published)


def test_default_call_system_prompt_only_uses_known_format_keys() -> None:
    """The prompt is ``.format()``-ed at call setup with three named
    fields. Any other ``{…}`` in the body would raise ``KeyError`` and
    kill the brain task before it ever drained a status event, which
    leaves the call record stuck at ``initiated``. Caught one real
    regression already (``confirm_and_end({summary…})`` in rule #6's
    example text). Belt-and-suspenders: this test renders the prompt
    with the exact same kwargs ``_run_call`` uses and just asserts no
    exception escapes."""
    rendered = _DEFAULT_CALL_SYSTEM_PROMPT.format(
        display_name="Test User",
        brief="Call them and confirm the booking",
        callback_number="+15551234567",
    )
    # Sanity-check substitution actually happened (catches the
    # accidentally-doubled ``{{display_name}}`` case which would render
    # the literal placeholder into the LLM context).
    assert "Test User" in rendered
    assert "{display_name}" not in rendered
    assert "{brief}" not in rendered
    assert "{callback_number}" not in rendered


def test_default_opening_disclosure_only_uses_known_format_keys() -> None:
    """Same hazard as the call system prompt — the opening disclosure
    is ``.format(display_name=…)``-ed at call setup and any stray ``{…}``
    would kill the brain. Smaller string but worth the same guard."""
    rendered = _DEFAULT_OPENING_DISCLOSURE.format(display_name="Test User")
    assert "Test User" in rendered
    assert "{display_name}" not in rendered


# ── Persistence shape ─────────────────────────────────────────────────


def test_call_record_roundtrips_through_dict() -> None:
    """``to_dict`` / ``_record_from_dict`` are how we persist + reload
    a call entity. Any silent field loss would corrupt the call detail
    page and break the callback-routing lookup."""
    record = _CallRecord(
        call_id="call_x",
        user_id="usr_jeremy",
        to_number="+13035550100",
        from_number="+17046411948",
        callback_number="+17046411948",
        brief="schedule audi service",
        status=CallStatus.CONNECTED.value,
        webhook_token="tok_secret",
        started_at="2026-05-23T15:30:12Z",
        ended_at="2026-05-23T15:38:47Z",
        duration_seconds=515.0,
        transcript=[{"who": "them", "text": "hi", "ts": 1.0}],
        outcome={"appointment_booked": True},
        failure_reason="",
        interventions=[{"who": "user", "text": "ask about loaner", "ts": 50.0}],
    )
    d = record.to_dict()
    # _id isn't included in to_dict (storage backend adds it)
    assert "_id" not in d
    reloaded = _record_from_dict(d, call_id="call_x")
    # Every field must round-trip.
    assert reloaded.call_id == record.call_id
    assert reloaded.user_id == record.user_id
    assert reloaded.to_number == record.to_number
    assert reloaded.from_number == record.from_number
    assert reloaded.callback_number == record.callback_number
    assert reloaded.brief == record.brief
    assert reloaded.status == record.status
    assert reloaded.webhook_token == record.webhook_token
    assert reloaded.started_at == record.started_at
    assert reloaded.ended_at == record.ended_at
    assert reloaded.duration_seconds == record.duration_seconds
    assert reloaded.transcript == record.transcript
    assert reloaded.outcome == record.outcome
    assert reloaded.interventions == record.interventions


def test_summarize_for_list_drops_full_transcript() -> None:
    """The list endpoint ships dozens of calls; carrying the full
    transcript on each would blow out the WS frame budget. The
    summary shape must stay small + keep the fields the SPA reads."""
    full = {
        "_id": "call_x",
        "user_id": "u",
        "to_number": "+1234567890",
        "status": "hung_up",
        "started_at": "2026-05-23T15:30:00Z",
        "ended_at": "2026-05-23T15:35:00Z",
        "duration_seconds": 300,
        "brief": "x" * 500,  # long
        "transcript": [{"who": "them", "text": "y" * 5000}],
        "outcome": {"k": "v"},
    }
    summary = _summarize_for_list(full)
    assert "transcript" not in summary
    assert summary["brief_preview"] == "x" * 120  # capped
    assert summary["call_id"] == "call_x"
    assert summary["duration_seconds"] == 300
    assert summary["outcome"] == {"k": "v"}


# ── Concurrency cap ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_call_refuses_when_user_has_active_call() -> None:
    """One active call per user is the policy (per spec). The second
    attempt must fail loudly rather than silently queueing or
    triggering a second carrier session."""
    svc = PhoneCallService()
    svc._enabled = True
    svc._backend = object()  # type: ignore[assignment]
    svc._from_number = "+15551234567"

    # Pretend a call is already active for this user.
    import asyncio

    from gilbert.core.services.phone_call import _ActiveCall

    fake_active = _ActiveCall(
        record=_CallRecord(
            call_id="existing",
            user_id="usr_x",
            to_number="+1000",
            from_number="+2000",
            callback_number="",
            brief="b",
            status="connected",
            webhook_token="t",
        ),
        session=None,  # type: ignore[arg-type]
        task=asyncio.create_task(asyncio.sleep(60)),
        interventions_queue=asyncio.Queue(),
    )
    svc._active["usr_x"] = fake_active
    try:
        with pytest.raises(RuntimeError, match="already have an active call"):
            await svc.start_call(
                user_id="usr_x",
                display_name="X",
                to_number="+3000",
                brief="another",
            )
    finally:
        fake_active.task.cancel()


@pytest.mark.asyncio
async def test_start_call_requires_from_number_to_be_configured() -> None:
    """No shared from-number = no calls. The spec mandates a single
    Telnyx number for v1; without it we can't set the caller-ID and
    Telnyx will reject ``place_call``. Better to fail at the tool
    boundary than waste a real API call."""
    svc = PhoneCallService()
    svc._enabled = True
    svc._backend = object()  # type: ignore[assignment]  # not None, but no from_number
    with pytest.raises(RuntimeError, match="from_number is not set"):
        await svc.start_call(
            user_id="usr_x",
            display_name="X",
            to_number="+3000",
            brief="b",
        )


@pytest.mark.asyncio
async def test_start_call_refuses_when_service_disabled() -> None:
    svc = PhoneCallService()
    # Default state: not enabled, no backend
    with pytest.raises(RuntimeError, match="not configured"):
        await svc.start_call(
            user_id="usr_x",
            display_name="X",
            to_number="+3000",
            brief="b",
        )


# ── Execute_tool error surface ────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_tool_rejects_unknown_tool_name() -> None:
    svc = PhoneCallService()
    with pytest.raises(KeyError):
        await svc.execute_tool("not_a_tool", {})


@pytest.mark.asyncio
async def test_execute_tool_requires_user_context() -> None:
    """The AI tool needs to know which user is placing the call (for
    the concurrency cap + the record's ``user_id``). Without a user on
    the async-local context (or with the SYSTEM placeholder) the call
    can't be attributed and we refuse."""
    svc = PhoneCallService()
    svc._enabled = True
    # No ``set_current_user`` call happened, so ``get_current_user``
    # returns ``UserContext.SYSTEM`` — exactly the case we reject.
    with pytest.raises(ValueError, match="user"):
        await svc.execute_tool(
            "make_phone_call",
            {"to_number": "+1234567890", "brief": "do a thing"},
        )
