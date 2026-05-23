"""Storage interface — generic entity store with queryability."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


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


class OnDelete(StrEnum):
    """Action to take when a referenced entity is deleted."""

    RESTRICT = "restrict"  # prevent deletion if references exist
    CASCADE = "cascade"  # delete referencing entities
    SET_NULL = "set_null"  # set the foreign key field to null


@dataclass
class ForeignKeyDefinition:
    """Defines a foreign key relationship between collections.

    The *field* in *collection* must reference an existing entity ID
    (or JSON field value) in *ref_collection*.
    """

    collection: str  # collection containing the FK field
    field: str  # dot-notation field path holding the reference
    ref_collection: str  # referenced collection
    ref_field: str = "_id"  # field in referenced collection ("_id" = entity ID)
    on_delete: OnDelete = OnDelete.RESTRICT
    name: str | None = None  # auto-generated if not provided


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

    @abstractmethod
    async def delete_query(self, query: Query) -> int:
        """Delete every row matching ``query``. Returns the count removed.

        Implementations SHOULD perform this as a single atomic operation
        where the underlying store supports it (one ``DELETE WHERE`` for
        SQLite). Cascading FK deletes still apply.

        Sort and offset on ``query`` are ignored — only filters select
        which rows are deleted. ``limit`` is honored when supported (the
        SQLite implementation translates it to ``LIMIT`` when present).
        """
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

    # --- Foreign Keys ---

    @abstractmethod
    async def ensure_foreign_key(self, fk: ForeignKeyDefinition) -> None:
        """Declare a foreign key constraint. Enforced on write and delete."""
        ...

    @abstractmethod
    async def list_foreign_keys(self, collection: str) -> list[ForeignKeyDefinition]:
        """List foreign key constraints involving a collection."""
        ...


@runtime_checkable
class StorageProvider(Protocol):
    """Protocol for accessing entity storage from a service.

    Services resolve this via ``get_capability("entity_storage")`` to
    access storage without depending on the concrete StorageService.
    """

    @property
    def backend(self) -> StorageBackend:
        """The default namespaced backend."""
        ...

    @property
    def raw_backend(self) -> StorageBackend:
        """The raw backend with no namespace prefix."""
        ...

    def create_namespaced(self, namespace: str) -> NamespacedStorageBackend:
        """Create a backend scoped to a custom namespace."""
        ...


class NamespacedStorageBackend(StorageBackend):
    """Wraps a StorageBackend with a transparent collection name prefix.

    All collection names are automatically prefixed with ``{namespace}.``
    so that multiple consumers (core services, plugins) can use the same
    bare collection names without collision.

    ``list_collections()`` returns only the collections belonging to this
    namespace, with the prefix stripped.
    """

    def __init__(self, inner: StorageBackend, namespace: str) -> None:
        self._inner = inner
        self._namespace = namespace
        self._prefix = f"{namespace}."

    @property
    def namespace(self) -> str:
        """The namespace this backend operates in."""
        return self._namespace

    @property
    def inner(self) -> StorageBackend:
        """The underlying unwrapped backend."""
        return self._inner

    def _ns(self, collection: str) -> str:
        """Prefix a collection name."""
        if collection.startswith(self._prefix):
            return collection
        return f"{self._prefix}{collection}"

    def _ns_query(self, query: Query) -> Query:
        """Return a copy of *query* with the collection name prefixed."""
        return Query(
            collection=self._ns(query.collection),
            filters=query.filters,
            sort=query.sort,
            limit=query.limit,
            offset=query.offset,
        )

    def _ns_index(self, index: IndexDefinition) -> IndexDefinition:
        """Return a copy of *index* with the collection name prefixed."""
        return IndexDefinition(
            collection=self._ns(index.collection),
            fields=index.fields,
            name=index.name,
            unique=index.unique,
        )

    def _ns_fk(self, fk: ForeignKeyDefinition) -> ForeignKeyDefinition:
        """Return a copy of *fk* with both collection names prefixed."""
        return ForeignKeyDefinition(
            collection=self._ns(fk.collection),
            field=fk.field,
            ref_collection=self._ns(fk.ref_collection),
            ref_field=fk.ref_field,
            on_delete=fk.on_delete,
            name=fk.name,
        )

    # --- Lifecycle (delegated, no prefixing) ---

    async def initialize(self) -> None:
        await self._inner.initialize()

    async def close(self) -> None:
        await self._inner.close()

    # --- Entity operations ---

    async def put(
        self,
        collection: str,
        entity_id: str,
        data: dict[str, Any],
    ) -> None:
        await self._inner.put(self._ns(collection), entity_id, data)

    async def get(
        self,
        collection: str,
        entity_id: str,
    ) -> dict[str, Any] | None:
        return await self._inner.get(self._ns(collection), entity_id)

    async def delete(self, collection: str, entity_id: str) -> None:
        await self._inner.delete(self._ns(collection), entity_id)

    async def exists(self, collection: str, entity_id: str) -> bool:
        return await self._inner.exists(self._ns(collection), entity_id)

    # --- Query operations ---

    async def query(self, query: Query) -> list[dict[str, Any]]:
        return await self._inner.query(self._ns_query(query))

    async def count(self, query: Query) -> int:
        return await self._inner.count(self._ns_query(query))

    async def delete_query(self, query: Query) -> int:
        return await self._inner.delete_query(self._ns_query(query))

    # --- Collection management ---

    async def list_collections(self) -> list[str]:
        all_cols = await self._inner.list_collections()
        return [c[len(self._prefix) :] for c in all_cols if c.startswith(self._prefix)]

    async def drop_collection(self, collection: str) -> None:
        await self._inner.drop_collection(self._ns(collection))

    # --- Indexing ---

    async def ensure_index(self, index: IndexDefinition) -> None:
        await self._inner.ensure_index(self._ns_index(index))

    async def list_indexes(self, collection: str) -> list[IndexDefinition]:
        return await self._inner.list_indexes(self._ns(collection))

    # --- Foreign keys ---

    async def ensure_foreign_key(self, fk: ForeignKeyDefinition) -> None:
        await self._inner.ensure_foreign_key(self._ns_fk(fk))

    async def list_foreign_keys(self, collection: str) -> list[ForeignKeyDefinition]:
        return await self._inner.list_foreign_keys(self._ns(collection))
