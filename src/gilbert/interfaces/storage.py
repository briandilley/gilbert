"""Storage interface — generic entity store with queryability."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FilterOp(StrEnum):
    """Comparison operators for query filters."""

    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    CONTAINS = "contains"
    EXISTS = "exists"


@dataclass
class Filter:
    """A single query filter on a field path."""

    field: str  # dot-notation path, e.g., "attributes.brightness"
    op: FilterOp
    value: Any = None


@dataclass
class SortField:
    """A sort directive."""

    field: str
    descending: bool = False


@dataclass
class Query:
    """A query against an entity collection."""

    collection: str
    filters: list[Filter] = field(default_factory=list)
    sort: list[SortField] = field(default_factory=list)
    limit: int | None = None
    offset: int = 0


@dataclass
class IndexDefinition:
    """Defines an index on a collection for efficient querying."""

    collection: str
    fields: list[str]  # dot-notation field paths to index
    name: str | None = None  # auto-generated if not provided
    unique: bool = False


class StorageBackend(ABC):
    """Abstract entity store. Implementation-agnostic."""

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the storage backend (create schema, etc.)."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    # --- Entity Operations ---

    @abstractmethod
    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        """Store an entity. Overwrites if it already exists."""
        ...

    @abstractmethod
    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        """Retrieve an entity by ID. Returns None if not found."""
        ...

    @abstractmethod
    async def delete(self, collection: str, entity_id: str) -> None:
        """Delete an entity by ID."""
        ...

    @abstractmethod
    async def exists(self, collection: str, entity_id: str) -> bool:
        """Check if an entity exists."""
        ...

    # --- Query Operations ---

    @abstractmethod
    async def query(self, query: Query) -> list[dict[str, Any]]:
        """Execute a query and return matching entities."""
        ...

    @abstractmethod
    async def count(self, query: Query) -> int:
        """Count entities matching a query."""
        ...

    # --- Collection Management ---

    @abstractmethod
    async def list_collections(self) -> list[str]:
        """List all known collections."""
        ...

    @abstractmethod
    async def drop_collection(self, collection: str) -> None:
        """Remove an entire collection and all its entities."""
        ...

    # --- Indexing ---

    @abstractmethod
    async def ensure_index(self, index: IndexDefinition) -> None:
        """Create an index if it doesn't exist. Integrations/plugins call this
        to declare how they plan to query entities."""
        ...

    @abstractmethod
    async def list_indexes(self, collection: str) -> list[IndexDefinition]:
        """List indexes on a collection."""
        ...
