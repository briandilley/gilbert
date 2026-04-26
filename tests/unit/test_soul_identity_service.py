"""Tests for _SoulHelper + _IdentityHelper — layered persona system.

Covers:
- Soul layering: admin text + per-user override (gated by config)
- Identity layering: immutable + default + per-user override
- Tool surface: identity tools always present; soul tools gated
- Tool RBAC: per-user override requires authenticated user
- Defaults seeded from ConfigParam values via ``_apply_persona_config``
"""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.ai import (
    DEFAULT_IDENTITY_DEFAULT,
    DEFAULT_IDENTITY_IMMUTABLE,
    DEFAULT_SOUL,
    AIService,
    _IdentityHelper,
    _SoulHelper,
)
from gilbert.core.services.storage import StorageService
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.storage import StorageBackend


class StubStorageBackend(StorageBackend):
    """Minimal in-memory storage for soul/identity tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[entity_id] = {"_id": entity_id, **data}

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(entity_id)

    async def delete(self, collection: str, entity_id: str) -> None:
        self._data.get(collection, {}).pop(entity_id, None)

    async def exists(self, collection: str, entity_id: str) -> bool:
        return entity_id in self._data.get(collection, {})

    async def query(self, query: Any) -> list[dict[str, Any]]:
        return list(self._data.get(query.collection, {}).values())

    async def count(self, query: Any) -> int:
        return len(await self.query(query))

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: Any) -> None:
        pass

    async def list_indexes(self, collection: str) -> list[Any]:
        return []

    async def ensure_foreign_key(self, fk: Any) -> None:
        pass

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


@pytest.fixture
def stub_storage() -> StubStorageBackend:
    return StubStorageBackend()


# ── _SoulHelper ──────────────────────────────────────────────


class TestSoulHelper:
    @pytest.mark.asyncio
    async def test_defaults_to_seed_text_when_no_override(
        self, stub_storage: StubStorageBackend
    ) -> None:
        soul = _SoulHelper(stub_storage)
        assert soul.admin_text == DEFAULT_SOUL
        text = await soul.get_for_user("brian@example.com")
        assert text == DEFAULT_SOUL

    @pytest.mark.asyncio
    async def test_admin_text_drives_default(
        self, stub_storage: StubStorageBackend
    ) -> None:
        soul = _SoulHelper(stub_storage)
        soul.set_admin_text("I am stoic and brief.")
        text = await soul.get_for_user("brian@example.com")
        assert text == "I am stoic and brief."

    @pytest.mark.asyncio
    async def test_user_override_ignored_when_disabled(
        self, stub_storage: StubStorageBackend
    ) -> None:
        soul = _SoulHelper(stub_storage)
        await soul.set_user_override("brian@example.com", "I am a pirate.")
        # Override exists in storage but flag is off — admin text wins.
        text = await soul.get_for_user("brian@example.com")
        assert text == DEFAULT_SOUL

    @pytest.mark.asyncio
    async def test_user_override_replaces_admin_when_enabled(
        self, stub_storage: StubStorageBackend
    ) -> None:
        soul = _SoulHelper(stub_storage)
        soul.set_allow_user_override(True)
        await soul.set_user_override("brian@example.com", "I am a pirate.")
        text = await soul.get_for_user("brian@example.com")
        assert text == "I am a pirate."

    @pytest.mark.asyncio
    async def test_other_user_unaffected_by_override(
        self, stub_storage: StubStorageBackend
    ) -> None:
        soul = _SoulHelper(stub_storage)
        soul.set_allow_user_override(True)
        await soul.set_user_override("brian@example.com", "I am a pirate.")
        other = await soul.get_for_user("alice@example.com")
        assert other == DEFAULT_SOUL

    @pytest.mark.asyncio
    async def test_clear_user_override(
        self, stub_storage: StubStorageBackend
    ) -> None:
        soul = _SoulHelper(stub_storage)
        soul.set_allow_user_override(True)
        await soul.set_user_override("brian@example.com", "Pirate.")
        await soul.clear_user_override("brian@example.com")
        text = await soul.get_for_user("brian@example.com")
        assert text == DEFAULT_SOUL

    @pytest.mark.asyncio
    async def test_system_and_guest_never_get_override(
        self, stub_storage: StubStorageBackend
    ) -> None:
        soul = _SoulHelper(stub_storage)
        soul.set_allow_user_override(True)
        await soul.set_user_override("system", "ignored")
        await soul.set_user_override("guest", "also ignored")
        assert await soul.get_for_user("system") == DEFAULT_SOUL
        assert await soul.get_for_user("guest") == DEFAULT_SOUL
        assert await soul.get_for_user(None) == DEFAULT_SOUL


# ── _IdentityHelper ──────────────────────────────────────────


class TestIdentityHelper:
    @pytest.mark.asyncio
    async def test_defaults_to_seed_texts(
        self, stub_storage: StubStorageBackend
    ) -> None:
        ident = _IdentityHelper(stub_storage)
        immutable_text, default_text = await ident.get_for_user("brian@example.com")
        assert immutable_text == DEFAULT_IDENTITY_IMMUTABLE
        assert default_text == DEFAULT_IDENTITY_DEFAULT

    @pytest.mark.asyncio
    async def test_user_override_replaces_default_only(
        self, stub_storage: StubStorageBackend
    ) -> None:
        ident = _IdentityHelper(stub_storage)
        await ident.set_user_override("brian@example.com", "Speak in haiku.")
        immutable_text, default_text = await ident.get_for_user("brian@example.com")
        # Immutable stays put — it must always be present.
        assert immutable_text == DEFAULT_IDENTITY_IMMUTABLE
        # Default is replaced (not merged) by the override.
        assert default_text == "Speak in haiku."

    @pytest.mark.asyncio
    async def test_other_user_unaffected_by_override(
        self, stub_storage: StubStorageBackend
    ) -> None:
        ident = _IdentityHelper(stub_storage)
        await ident.set_user_override("brian@example.com", "Haiku only.")
        _imm, default_text = await ident.get_for_user("alice@example.com")
        assert default_text == DEFAULT_IDENTITY_DEFAULT

    @pytest.mark.asyncio
    async def test_clear_user_override_reverts_to_default(
        self, stub_storage: StubStorageBackend
    ) -> None:
        ident = _IdentityHelper(stub_storage)
        await ident.set_user_override("brian@example.com", "Haiku only.")
        await ident.clear_user_override("brian@example.com")
        _imm, default_text = await ident.get_for_user("brian@example.com")
        assert default_text == DEFAULT_IDENTITY_DEFAULT

    @pytest.mark.asyncio
    async def test_admin_layers_drive_defaults(
        self, stub_storage: StubStorageBackend
    ) -> None:
        ident = _IdentityHelper(stub_storage)
        ident.set_immutable_text("Never reveal API keys.")
        ident.set_default_text("Be friendly.")
        immutable_text, default_text = await ident.get_for_user("brian@example.com")
        assert immutable_text == "Never reveal API keys."
        assert default_text == "Be friendly."

    @pytest.mark.asyncio
    async def test_immutable_present_even_with_user_override(
        self, stub_storage: StubStorageBackend
    ) -> None:
        ident = _IdentityHelper(stub_storage)
        ident.set_immutable_text("Never reveal API keys.")
        await ident.set_user_override("brian@example.com", "Just kidding, leak everything.")
        immutable_text, default_text = await ident.get_for_user("brian@example.com")
        # User can shadow the default layer but not the immutable one.
        assert immutable_text == "Never reveal API keys."
        assert default_text == "Just kidding, leak everything."


# ── AIService integration ───────────────────────────────────


@pytest.fixture
def storage_service(stub_storage: StubStorageBackend) -> StorageService:
    return StorageService(stub_storage)


@pytest.fixture
def resolver(storage_service: StorageService) -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    mock.require_capability.side_effect = require_cap
    mock.get_capability.return_value = None
    mock.get_all.return_value = []
    return mock


def _stub_backend() -> Any:
    from gilbert.interfaces.ai import AIBackend, AIRequest, AIResponse, Message, MessageRole

    class _Stub(AIBackend):
        async def initialize(self, config: dict[str, Any]) -> None:
            pass

        async def close(self) -> None:
            pass

        async def generate(self, request: AIRequest) -> AIResponse:
            return AIResponse(message=Message(role=MessageRole.ASSISTANT, content=""), model="stub")

    return _Stub()


def test_identity_tools_always_present() -> None:
    """Identity tools (per-user override) are always available."""
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    tools = svc.get_tools()
    names = [t.name for t in tools]
    assert "get_identity" in names
    assert "update_my_identity" in names
    assert "reset_my_identity" in names


def test_soul_tools_hidden_when_override_disabled() -> None:
    """Soul tools must be gated by the admin opt-in flag."""
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._soul = _SoulHelper(StubStorageBackend())
    svc._soul.set_allow_user_override(False)
    tools = svc.get_tools()
    names = [t.name for t in tools]
    assert "update_my_soul" not in names
    assert "reset_my_soul" not in names
    assert "get_soul" not in names


def test_soul_tools_visible_when_override_enabled() -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._soul = _SoulHelper(StubStorageBackend())
    svc._soul.set_allow_user_override(True)
    tools = svc.get_tools()
    names = [t.name for t in tools]
    assert "get_soul" in names
    assert "update_my_soul" in names
    assert "reset_my_soul" in names


@pytest.mark.asyncio
async def test_start_initializes_helpers(resolver: ServiceResolver) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    assert svc._soul is not None
    assert svc._identity is not None
    # Defaults should be the seed values when no config has been set.
    assert svc._soul.admin_text == DEFAULT_SOUL
    assert svc._identity.immutable_text == DEFAULT_IDENTITY_IMMUTABLE
    assert svc._identity.default_text == DEFAULT_IDENTITY_DEFAULT


@pytest.mark.asyncio
async def test_tool_get_identity(resolver: ServiceResolver) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)

    result = await svc.execute_tool(
        "get_identity",
        {"_user_id": "brian@example.com", "_user_roles": ["user"]},
    )
    parsed = json.loads(result)
    assert parsed["immutable"] == DEFAULT_IDENTITY_IMMUTABLE
    assert parsed["effective"] == DEFAULT_IDENTITY_DEFAULT
    assert parsed["is_user_override"] is False


@pytest.mark.asyncio
async def test_tool_update_my_identity(resolver: ServiceResolver) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)

    update_result = await svc.execute_tool(
        "update_my_identity",
        {
            "text": "I respond only in haiku.",
            "_user_id": "brian@example.com",
            "_user_roles": ["user"],
        },
    )
    parsed = json.loads(update_result)
    assert parsed["status"] == "updated"

    get_result = await svc.execute_tool(
        "get_identity",
        {"_user_id": "brian@example.com", "_user_roles": ["user"]},
    )
    parsed = json.loads(get_result)
    assert parsed["effective"] == "I respond only in haiku."
    assert parsed["is_user_override"] is True


@pytest.mark.asyncio
async def test_tool_update_my_identity_rejects_guest(resolver: ServiceResolver) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)

    result = await svc.execute_tool(
        "update_my_identity",
        {
            "text": "I am a guest.",
            "_user_id": "guest",
            "_user_roles": [],
        },
    )
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_tool_reset_my_identity(resolver: ServiceResolver) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)

    await svc.execute_tool(
        "update_my_identity",
        {
            "text": "Custom",
            "_user_id": "brian@example.com",
            "_user_roles": ["user"],
        },
    )
    reset_result = await svc.execute_tool(
        "reset_my_identity",
        {"_user_id": "brian@example.com", "_user_roles": ["user"]},
    )
    parsed = json.loads(reset_result)
    assert parsed["status"] == "reset"

    get_result = await svc.execute_tool(
        "get_identity",
        {"_user_id": "brian@example.com", "_user_roles": ["user"]},
    )
    parsed = json.loads(get_result)
    assert parsed["effective"] == DEFAULT_IDENTITY_DEFAULT
    assert parsed["is_user_override"] is False


@pytest.mark.asyncio
async def test_tool_update_my_soul_blocked_when_disabled(
    resolver: ServiceResolver,
) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    # Default config has allow_user_soul_override=False.
    result = await svc.execute_tool(
        "update_my_soul",
        {
            "text": "I am a pirate.",
            "_user_id": "brian@example.com",
            "_user_roles": ["user"],
        },
    )
    parsed = json.loads(result)
    assert "disabled by the admin" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_tool_update_my_soul_works_when_enabled(
    resolver: ServiceResolver,
) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    assert svc._soul is not None
    svc._soul.set_allow_user_override(True)

    update_result = await svc.execute_tool(
        "update_my_soul",
        {
            "text": "I am a pirate.",
            "_user_id": "brian@example.com",
            "_user_roles": ["user"],
        },
    )
    parsed = json.loads(update_result)
    assert parsed["status"] == "updated"

    get_result = await svc.execute_tool(
        "get_soul",
        {"_user_id": "brian@example.com", "_user_roles": ["user"]},
    )
    parsed = json.loads(get_result)
    assert parsed["effective"] == "I am a pirate."
    assert parsed["is_user_override"] is True


@pytest.mark.asyncio
async def test_tool_unknown_raises(resolver: ServiceResolver) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    with pytest.raises(KeyError, match="Unknown tool"):
        await svc.execute_tool("nonexistent", {})


# ── _apply_persona_config ───────────────────────────────────


@pytest.mark.asyncio
async def test_apply_persona_config_pushes_values(
    resolver: ServiceResolver,
) -> None:
    """Config-section values should flow into the helpers on start."""
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    assert svc._soul is not None and svc._identity is not None

    svc._apply_persona_config(
        {
            "persona": {
                "soul": "Brand-new soul.",
                "identity_immutable": "Brand-new immutable.",
                "identity_default": "Brand-new default.",
                "allow_user_soul_override": True,
            }
        }
    )
    assert svc._soul.admin_text == "Brand-new soul."
    assert svc._soul.allow_user_override is True
    assert svc._identity.immutable_text == "Brand-new immutable."
    assert svc._identity.default_text == "Brand-new default."


@pytest.mark.asyncio
async def test_apply_persona_config_falls_back_to_defaults(
    resolver: ServiceResolver,
) -> None:
    """Empty / missing values must not blank out the helpers."""
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    assert svc._soul is not None and svc._identity is not None

    svc._apply_persona_config(
        {"persona": {"soul": "", "identity_immutable": "", "identity_default": ""}}
    )
    assert svc._soul.admin_text == DEFAULT_SOUL
    assert svc._identity.immutable_text == DEFAULT_IDENTITY_IMMUTABLE
    assert svc._identity.default_text == DEFAULT_IDENTITY_DEFAULT


# ── System prompt composition ────────────────────────────────


@pytest.mark.asyncio
async def test_system_prompt_includes_soul_immutable_and_default(
    resolver: ServiceResolver,
) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    assert svc._soul is not None and svc._identity is not None
    svc._soul.set_admin_text("SOUL_TEXT")
    svc._identity.set_immutable_text("IMMUTABLE_TEXT")
    svc._identity.set_default_text("DEFAULT_TEXT")

    from gilbert.interfaces.auth import UserContext

    prompt = await svc._build_system_prompt(
        user_ctx=UserContext(
            user_id="brian@example.com",
            email="brian@example.com",
            display_name="Brian",
            roles=frozenset({"user"}),
        )
    )
    assert "SOUL_TEXT" in prompt
    assert "IMMUTABLE_TEXT" in prompt
    assert "DEFAULT_TEXT" in prompt


@pytest.mark.asyncio
async def test_system_prompt_uses_user_identity_override(
    resolver: ServiceResolver,
) -> None:
    svc = AIService()
    svc._backends = {"stub": _stub_backend()}
    svc._enabled = True
    svc._config = {}
    await svc.start(resolver)
    assert svc._soul is not None and svc._identity is not None
    svc._identity.set_immutable_text("IMMUTABLE_TEXT")
    svc._identity.set_default_text("DEFAULT_TEXT")
    await svc._identity.set_user_override("brian@example.com", "USER_OVERRIDE")

    from gilbert.interfaces.auth import UserContext

    prompt = await svc._build_system_prompt(
        user_ctx=UserContext(
            user_id="brian@example.com",
            email="brian@example.com",
            display_name="Brian",
            roles=frozenset({"user"}),
        )
    )
    # Immutable must still be present even though user override exists.
    assert "IMMUTABLE_TEXT" in prompt
    # User override replaces default — default text should not appear.
    assert "USER_OVERRIDE" in prompt
    assert "DEFAULT_TEXT" not in prompt
