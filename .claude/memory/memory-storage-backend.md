# Storage Backend

## Summary
Generic entity store abstracted behind `StorageBackend` ABC. SQLite implementation uses JSON columns — no SQL-shaped API, no migrations needed for new entity types. Service capability is `entity_storage`.

## Details
The storage interface (`src/gilbert/interfaces/storage.py`) exposes:
- **Entity CRUD**: `put(collection, id, data)`, `get()`, `delete()`, `exists()`
- **Querying**: `query(Query)` and `count(Query)` using `Filter` objects with operators (eq, neq, gt, gte, lt, lte, in, contains, exists)
- **Collection management**: `list_collections()`, `drop_collection()`
- **Indexing**: `ensure_index(IndexDefinition)` — integrations declare how they query, and the backend creates appropriate indexes

SQLite implementation (`src/gilbert/storage/sqlite.py`):
- Each collection is a table with `(id TEXT PK, data TEXT)` where data is JSON
- Queries use `json_extract(data, '$.field')` for filtering and sorting
- Indexes use `json_extract` expressions for efficient lookups
- Collections are auto-created on first `put()`
- `_id` is a virtual field in query results containing the entity ID

Design decision: storage returns `dict[str, Any]` — it's intentionally untyped. Typed interpretation happens in the layer above. This keeps the storage interface stable as domain models evolve.

## Related
- `src/gilbert/interfaces/storage.py` — ABC and query types (Filter, FilterOp, Query, SortField, IndexDefinition)
- `src/gilbert/storage/sqlite.py` — SQLite JSON entity store implementation
- `tests/integration/test_sqlite_storage.py` — 29 integration tests against real SQLite
