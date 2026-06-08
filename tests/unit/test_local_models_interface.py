"""Unit tests for the local-model runtime capability protocol."""

from __future__ import annotations

from gilbert.interfaces.local_models import (
    InstalledModel,
    LocalModelRuntimeProvider,
)


def test_installed_model_defaults() -> None:
    m = InstalledModel(tag="llama3.3")
    assert m.tag == "llama3.3"
    assert m.size_bytes is None


def test_installed_model_with_size() -> None:
    m = InstalledModel(tag="qwen2.5-coder:32b", size_bytes=19_000_000_000)
    assert m.size_bytes == 19_000_000_000


def test_provider_is_runtime_checkable() -> None:
    """The protocol must be usable in ``isinstance`` checks so consumers
    can narrow a resolved service to it without importing a concrete class."""
    assert hasattr(LocalModelRuntimeProvider, "_is_runtime_protocol")


def test_minimal_stub_satisfies_protocol() -> None:
    """A stub implementing the four members must pass ``isinstance``; one
    missing a member must fail."""

    class _Runtime:
        async def list_models(self) -> list[InstalledModel]:
            return [InstalledModel(tag="llama3.3", size_bytes=1)]

        async def pull_model(self, ref: str) -> None:
            return None

        async def delete_model(self, tag: str) -> None:
            return None

        def base_url(self) -> str:
            return "http://localhost:11434"

    assert isinstance(_Runtime(), LocalModelRuntimeProvider)

    class _Partial:
        async def list_models(self) -> list[InstalledModel]:
            return []

    assert not isinstance(_Partial(), LocalModelRuntimeProvider)
