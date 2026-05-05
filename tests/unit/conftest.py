"""Shared fixtures for unit tests under tests/unit/."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.core.events import InMemoryEventBus
from gilbert.storage.sqlite import SQLiteStorage

# ── Minimal fakes that satisfy Protocol isinstance checks ────────────


class _FakeStorageProvider:
    """Satisfies StorageProvider (has .backend)."""

    def __init__(self, backend: SQLiteStorage) -> None:
        self._backend = backend

    @property
    def backend(self) -> SQLiteStorage:
        return self._backend

    @property
    def raw_backend(self) -> SQLiteStorage:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:  # noqa: ANN401
        return self._backend


class _FakeEventBusProvider:
    """Satisfies EventBusProvider (has .bus)."""

    def __init__(self) -> None:
        self.bus = InMemoryEventBus()


class _FakeAIProvider:
    """Satisfies AIProvider (has .chat) and AIToolDiscoveryProvider.

    Returns a minimal ChatTurnResult so run_agent_now tests succeed without
    a real AI backend. The turn_usage keys mirror ChatTurnResult's dict shape:
    input_tokens / output_tokens / cost_usd / rounds.

    Records the kwargs from the most recent chat() call in last_call_kwargs
    so tests can assert on system_prompt and other arguments.

    ``discover_tools`` returns an empty dict by default; tests that need a
    populated tool catalog can monkeypatch ``svc._tool_discovery`` directly.

    Optional Phase 2 hooks:

    - ``invoke_between_rounds`` — when True, ``chat`` invokes any
      ``between_rounds_callback`` once before returning. The callback's
      result is captured in ``last_between_rounds_result`` so tests can
      inspect the messages the agent service produced.
    - ``response_text`` — overrides the response text returned by chat
      (default ``"ok"``).
    - ``chat_delay_s`` — number of seconds to sleep before returning.
      Used to test delegation timeouts.
    - ``raise_on_chat`` — when set to an exception instance, ``chat``
      raises it before returning. Used to test delegation failure paths.
    """

    def __init__(self) -> None:
        self.last_call_kwargs: dict[str, Any] = {}
        self.invoke_between_rounds: bool = False
        self.last_between_rounds_result: list[Any] | None = None
        self.response_text: str = "ok"
        self.chat_delay_s: float = 0.0
        self.raise_on_chat: BaseException | None = None
        self.chat_call_count: int = 0

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        self.chat_call_count += 1
        self.last_call_kwargs = dict(kwargs)

        if self.invoke_between_rounds:
            cb = kwargs.get("between_rounds_callback")
            if cb is not None:
                self.last_between_rounds_result = await cb()

        if self.chat_delay_s > 0.0:
            import asyncio
            await asyncio.sleep(self.chat_delay_s)

        if self.raise_on_chat is not None:
            raise self.raise_on_chat

        from gilbert.interfaces.ai import ChatTurnResult
        return ChatTurnResult(
            response_text=self.response_text,
            conversation_id="conv_test",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
            interrupted=False,
            model="",
            turn_usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
                "rounds": 1,
            },
        )

    def discover_tools(
        self, *, user_ctx: Any = None, profile_name: str | None = None,  # noqa: ANN401
    ) -> dict[str, Any]:
        return {}


class _FakeSchedulerProvider:
    """Satisfies SchedulerProvider (all required methods present).

    Tracks add_job and remove_job calls so heartbeat tests can assert
    on registered and removed job names.
    """

    def __init__(self) -> None:
        self.added_jobs: list[str] = []
        self.removed_jobs: list[str] = []
        self._jobs: dict[str, Any] = {}

    def add_job(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        name: str = kwargs.get("name", args[0] if args else "")
        if name in self._jobs:
            raise ValueError(f"Job '{name}' already registered")
        self.added_jobs.append(name)
        self._jobs[name] = kwargs

    def remove_job(self, name: str, requester_id: str = "") -> None:
        if name not in self._jobs:
            raise KeyError(f"Job not found: {name}")
        self.removed_jobs.append(name)
        self._jobs.pop(name, None)

    def enable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def disable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def list_jobs(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def get_job(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return None

    async def run_now(self, *args: Any, **kwargs: Any) -> None:
        pass


def _make_resolver(**caps: Any) -> Any:
    """Build a minimal ServiceResolver that returns the given capabilities."""

    class _Resolver:
        def require_capability(self, name: str) -> Any:
            if name not in caps:
                raise LookupError(name)
            return caps[name]

        def get_capability(self, name: str) -> Any:
            return caps.get(name)

        def get_all(self, name: str) -> list[Any]:
            return []

    return _Resolver()


# ── Shared AgentService fixture ──────────────────────────────────────


@pytest.fixture
async def started_agent_service(sqlite_storage: SQLiteStorage) -> Any:
    """Start an AgentService backed by a real SQLite database."""
    from gilbert.core.services.agent import AgentService

    storage_provider = _FakeStorageProvider(sqlite_storage)
    event_bus_provider = _FakeEventBusProvider()
    ai_provider = _FakeAIProvider()
    scheduler_provider = _FakeSchedulerProvider()

    resolver = _make_resolver(
        entity_storage=storage_provider,
        event_bus=event_bus_provider,
        ai_chat=ai_provider,
        scheduler=scheduler_provider,
    )

    svc = AgentService()
    await svc.start(resolver)
    yield svc
    await svc.stop()
