"""Tests for _MemoryHelper — per-user persistent memories (via AIService)."""

from typing import Any

import pytest

from gilbert.core.services.ai import AIService, _MemoryHelper
from gilbert.interfaces.auth import UserContext

# ── Fake storage ────────────────────────────────────────────


class FakeStorageBackend:
    """In-memory storage backend for testing."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._indexes: list[Any] = []

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        record = self._data.get(collection, {}).get(key)
        if record is not None:
            return {**record, "_id": key}
        return None

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = data

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def query(self, query: Any) -> list[dict[str, Any]]:
        collection = query.collection
        results = []
        for key, data in self._data.get(collection, {}).items():
            record = {**data, "_id": key}
            # Apply filters
            match = True
            for f in query.filters or []:
                if record.get(f.field) != f.value:
                    match = False
                    break
            if match:
                results.append(record)
        return results

    async def ensure_index(self, index_def: Any) -> None:
        self._indexes.append(index_def)


class FakeStorageService:
    def __init__(self) -> None:
        self.backend = FakeStorageBackend()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        return self.caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


def _set_user(user_id: str = "brian@example.com") -> UserContext:
    """Create and set a test user context."""
    user = UserContext(
        user_id=user_id,
        email=user_id,
        display_name="Brian",
        roles=frozenset({"user"}),
    )
    from gilbert.core.context import set_current_user

    set_current_user(user)
    return user


@pytest.fixture
def fake_storage() -> FakeStorageBackend:
    return FakeStorageBackend()


@pytest.fixture
def helper(fake_storage: FakeStorageBackend) -> _MemoryHelper:
    return _MemoryHelper(fake_storage)


@pytest.fixture
async def started_helper(helper: _MemoryHelper) -> _MemoryHelper:
    await helper.setup_indexes()
    return helper


# ── Tests ───────────────────────────────────────────────────


class TestMemoryHelper:
    def test_ai_service_has_memory_capability(self) -> None:
        svc = AIService()
        info = svc.service_info()
        assert "user_memory" in info.capabilities

    def test_ai_service_has_memory_tool(self) -> None:
        from gilbert.interfaces.ai import AIBackend, AIRequest, AIResponse, Message, MessageRole

        class _Stub(AIBackend):
            async def initialize(self, config: dict[str, Any]) -> None:
                pass

            async def close(self) -> None:
                pass

            async def generate(self, request: AIRequest) -> AIResponse:
                return AIResponse(
                    message=Message(role=MessageRole.ASSISTANT, content=""), model="stub"
                )

        svc = AIService()
        svc._backends = {"stub": _Stub()}
        svc._enabled = True
        tools = svc.get_tools()
        assert any(t.name == "memory" for t in tools)
        memory_tool = next(t for t in tools if t.name == "memory")
        action_param = next(p for p in memory_tool.parameters if p.name == "action")
        assert set(action_param.enum) == {"remember", "recall", "update", "forget", "list"}

    @pytest.mark.asyncio
    async def test_remember(self, started_helper: _MemoryHelper) -> None:
        _set_user("brian@example.com")
        result = await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Prefers metric units",
                "content": "Brian prefers metric units for all measurements",
                "source": "user",
            },
        )
        assert "remember" in result.lower()

    @pytest.mark.asyncio
    async def test_list(self, started_helper: _MemoryHelper) -> None:
        _set_user("brian@example.com")
        await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Likes coffee",
                "content": "Brian likes strong black coffee",
            },
        )
        result = await started_helper.list_memories("brian@example.com")
        assert "1 memory" in result
        assert "Likes coffee" in result

    @pytest.mark.asyncio
    async def test_recall(self, started_helper: _MemoryHelper) -> None:
        _set_user("brian@example.com")
        result = await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Test memory",
                "content": "Detailed content here",
            },
        )
        # Extract memory ID from result
        memory_id = result.split("memory ")[-1].rstrip(")")
        recall_result = await started_helper.recall(
            "brian@example.com",
            {
                "ids": [memory_id],
            },
        )
        assert "Detailed content here" in recall_result
        assert "Accessed: 1 times" in recall_result

    @pytest.mark.asyncio
    async def test_update(self, started_helper: _MemoryHelper) -> None:
        _set_user("brian@example.com")
        result = await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Old summary",
                "content": "Old content",
            },
        )
        memory_id = result.split("memory ")[-1].rstrip(")")
        update_result = await started_helper.update(
            "brian@example.com",
            {
                "id": memory_id,
                "summary": "New summary",
            },
        )
        assert "updated" in update_result.lower()

    @pytest.mark.asyncio
    async def test_forget(self, started_helper: _MemoryHelper) -> None:
        _set_user("brian@example.com")
        result = await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Temporary",
                "content": "Will be forgotten",
            },
        )
        memory_id = result.split("memory ")[-1].rstrip(")")
        forget_result = await started_helper.forget(
            "brian@example.com",
            {
                "id": memory_id,
            },
        )
        assert "forgotten" in forget_result.lower()

        # List should be empty now
        list_result = await started_helper.list_memories("brian@example.com")
        assert "no memories" in list_result.lower()

    @pytest.mark.asyncio
    async def test_ownership_isolation(self, started_helper: _MemoryHelper) -> None:
        result = await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Brian's memory",
                "content": "Private stuff",
            },
        )
        memory_id = result.split("memory ")[-1].rstrip(")")

        forget_result = await started_helper.forget(
            "alice@example.com",
            {
                "id": memory_id,
            },
        )
        assert "doesn't belong" in forget_result.lower()

        # Alice's list should be empty
        list_result = await started_helper.list_memories("alice@example.com")
        assert "no memories" in list_result.lower()

    @pytest.mark.asyncio
    async def test_get_user_summaries(self, started_helper: _MemoryHelper) -> None:
        await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Prefers metric",
                "content": "Uses metric units",
            },
        )
        await started_helper.remember(
            "brian@example.com",
            {
                "summary": "Drives a Tesla",
                "content": "Has a Model 3",
            },
        )
        summaries = await started_helper.get_user_summaries("brian@example.com")
        assert "Prefers metric" in summaries
        assert "Drives a Tesla" in summaries
        assert "2 stored" in summaries

    @pytest.mark.asyncio
    async def test_get_user_summaries_empty(self, started_helper: _MemoryHelper) -> None:
        result = await started_helper.get_user_summaries("nobody@example.com")
        assert result == ""


# ── Tool dispatcher tests ───────────────────────────────────
#
# These exercise ``AIService._tool_memory_action``, which is the entry
# point invoked by the AI tool executor and the slash-command dispatcher.
# Those executors pass caller identity via an injected ``_user_id`` key
# in the arguments dict (see ``_execute_tool_calls`` and
# ``_invoke_slash_command``) — they do **not** set the ``get_current_user``
# contextvar. The handler must honor the injected value or every
# ``/memory`` invocation fails with "Memory requires an authenticated user".


@pytest.fixture
def ai_service_with_memory(fake_storage: FakeStorageBackend) -> AIService:
    svc = AIService()
    svc._storage = fake_storage  # type: ignore[assignment]
    svc._memory = _MemoryHelper(fake_storage)
    return svc


class TestToolMemoryAction:
    @pytest.mark.asyncio
    async def test_remember_uses_injected_user_id(
        self,
        ai_service_with_memory: AIService,
    ) -> None:
        # Ensure contextvar is the SYSTEM default — i.e. nothing has set
        # it for this call. This mirrors the real chat flow, where the
        # WS handler does not populate the contextvar before dispatching
        # tool calls.
        from gilbert.core.context import set_current_user

        set_current_user(UserContext.SYSTEM)

        result = await ai_service_with_memory._tool_memory_action(
            {
                "action": "remember",
                "summary": "Brian's EIN for Current Electric Vehicles",
                "content": "EIN 87-4708791",
                "source": "user",
                "_user_id": "brian@example.com",
                "_user_name": "Brian",
                "_user_roles": ["user"],
            }
        )
        assert "remember" in result.lower()
        assert "authenticated user" not in result.lower()

        list_result = await ai_service_with_memory._tool_memory_action(
            {
                "action": "list",
                "_user_id": "brian@example.com",
            }
        )
        assert "Brian's EIN" in list_result

    @pytest.mark.asyncio
    async def test_rejects_system_user_when_no_injected_id(
        self,
        ai_service_with_memory: AIService,
    ) -> None:
        from gilbert.core.context import set_current_user

        set_current_user(UserContext.SYSTEM)

        result = await ai_service_with_memory._tool_memory_action(
            {
                "action": "list",
            }
        )
        assert "authenticated user" in result.lower()

    @pytest.mark.asyncio
    async def test_falls_back_to_contextvar(
        self,
        ai_service_with_memory: AIService,
    ) -> None:
        # Direct callers (tests, future non-tool entry points) that set
        # the contextvar should still work without having to supply
        # ``_user_id`` in the arguments dict.
        _set_user("brian@example.com")
        try:
            result = await ai_service_with_memory._tool_memory_action(
                {
                    "action": "remember",
                    "summary": "Likes pour-over",
                    "content": "Brian prefers pour-over coffee",
                }
            )
            assert "remember" in result.lower()
        finally:
            from gilbert.core.context import set_current_user

            set_current_user(UserContext.SYSTEM)
