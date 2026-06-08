"""Local-model runtime capability — drive a local LLM runtime (Ollama, …).

A *local model runtime* is the server/quantizer that actually holds the
open-weight model tags on disk and serves inference (today: Ollama). The
``LocalModelRuntimeProvider`` capability lets another service — e.g. the
local-model **manager** plugin — list / pull / delete installed tags and
learn the runtime's resolved ``base_url`` **without reading the AI
backend's config or coupling to the concrete Ollama plugin**. A future
runtime could replace Ollama unchanged so long as it advertises this
capability.

Kept deliberately tiny and provider-neutral: a tag string plus an
optional on-disk size is all the manager needs to render the
"installed" list and a delete button; everything richer (HF catalog,
hardware-fit) lives in the manager, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "InstalledModel",
    "LocalModelRuntimeProvider",
]


@dataclass(frozen=True)
class InstalledModel:
    """A single model tag installed in the local runtime.

    - ``tag`` — the runtime-local model reference (e.g. ``"llama3.3"`` or
      ``"qwen2.5-coder:32b"``). This is the exact string a chat request
      selects and that ``delete_model`` takes.
    - ``size_bytes`` — on-disk size in bytes when the runtime reports it,
      else ``None`` ("unknown"). The manager uses it for the installed
      list and to estimate reclaimable disk on delete.
    """

    tag: str
    size_bytes: int | None = None


@runtime_checkable
class LocalModelRuntimeProvider(Protocol):
    """Capability for driving a local LLM runtime's installed models.

    Resolved by name via ``resolver.get_capability("local_model_runtime")``
    and narrowed with ``isinstance(svc, LocalModelRuntimeProvider)``. The
    provider owns where the runtime lives (``base_url()``), so consumers
    never re-enter the URL or read the AI backend's storage.
    """

    async def list_models(self) -> list[InstalledModel]:
        """Return every model tag currently installed in the runtime."""
        ...

    async def pull_model(self, ref: str) -> None:
        """Install a model into the runtime, blocking until it completes.

        ``ref`` is whatever the runtime accepts as a pullable reference —
        a registry tag (``"llama3.3"``) or a Hugging Face GGUF reference
        (``"hf.co/<repo>:<quant>"``). Raises on failure.
        """
        ...

    async def delete_model(self, tag: str) -> None:
        """Remove an installed model tag, reclaiming its disk. Raises on failure."""
        ...

    def base_url(self) -> str:
        """Return the runtime's resolved base URL (e.g. the Ollama server)."""
        ...
