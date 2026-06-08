# Generic entity store with a non-SQL query interface; new entity types need no migrations

Persistence is a generic store of typed entity collections queried through a query interface, not a
relational schema. Adding a new entity type requires no migration, and the underlying storage
backend (SQLite today) is swappable behind the `StorageBackend` interface.

## Consequences

- Plugins and services add collections freely — extensibility without schema churn is the whole
  point.
- Queries are **not** SQL-shaped; there are no relational joins or foreign-key constraints. Code
  that needs cross-collection relationships composes them in application code.
- Migrations still exist for *data* transformations (`migrations/NNNN_*.py`) and must be idempotent
  — but adding a field or a collection is not one of them.
