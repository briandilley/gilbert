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
    """Satisfies AIProvider (has .chat).

    Returns a minimal ChatTurnResult so run_agent_now tests succeed without
    a real AI backend. The turn_usage keys mirror ChatTurnResult's dict shape:
    input_tokens / output_tokens / cost_usd / rounds.
    """

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        from gilbert.interfaces.ai import ChatTurnResult
        return ChatTurnResult(
            response_text="ok",
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


class _FakeSchedulerProvider:
    """Satisfies SchedulerProvider (all required methods present)."""

    def add_job(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        pass

    def remove_job(self, *args: Any, **kwargs: Any) -> None:
        pass

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
