"""SQLite implementation of StorageBackend using JSON entity storage."""

import json
from typing import Any

import aiosqlite

from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
)


class SQLiteStorage(StorageBackend):
    """SQLite-backed entity store using JSON columns.

    Each collection is stored in a single table with (id, data) columns
    where data is a JSON blob. Indexes use SQLite's json_extract to
    index specific field paths within the JSON.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._known_collections: set[str] = set()

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage not initialized. Call initialize() first.")
        return self._db

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS _collections (
                name TEXT PRIMARY KEY
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS _indexes (
                name TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                fields TEXT NOT NULL,
                is_unique INTEGER NOT NULL DEFAULT 0
            )
        """)
        await self._db.commit()

        # Load known collections
        async with self._db.execute("SELECT name FROM _collections") as cursor:
            rows = await cursor.fetchall()
            self._known_collections = {row[0] for row in rows}

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # --- Collection Management ---

    async def _ensure_collection_table(self, collection: str) -> None:
        if collection in self._known_collections:
            return
        db = await self._conn()
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS "{self._table_name(collection)}" (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL DEFAULT '{{}}'
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO _collections (name) VALUES (?)", (collection,)
        )
        await db.commit()
        self._known_collections.add(collection)

    @staticmethod
    def _table_name(collection: str) -> str:
        return f"c_{collection}"

    async def list_collections(self) -> list[str]:
        return sorted(self._known_collections)

    async def drop_collection(self, collection: str) -> None:
        db = await self._conn()
        table = self._table_name(collection)
        await db.execute(f'DROP TABLE IF EXISTS "{table}"')
        await db.execute("DELETE FROM _collections WHERE name = ?", (collection,))
        await db.execute("DELETE FROM _indexes WHERE collection = ?", (collection,))
        await db.commit()
        self._known_collections.discard(collection)

    # --- Entity Operations ---

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        await self._ensure_collection_table(collection)
        db = await self._conn()
        table = self._table_name(collection)
        await db.execute(
            f'INSERT OR REPLACE INTO "{table}" (id, data) VALUES (?, ?)',
            (entity_id, json.dumps(data)),
        )
        await db.commit()

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        if collection not in self._known_collections:
            return None
        db = await self._conn()
        table = self._table_name(collection)
        async with db.execute(
            f'SELECT data FROM "{table}" WHERE id = ?', (entity_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            result: dict[str, Any] = json.loads(row[0])
            return result

    async def delete(self, collection: str, entity_id: str) -> None:
        if collection not in self._known_collections:
            return
        db = await self._conn()
        table = self._table_name(collection)
        await db.execute(f'DELETE FROM "{table}" WHERE id = ?', (entity_id,))
        await db.commit()

    async def exists(self, collection: str, entity_id: str) -> bool:
        if collection not in self._known_collections:
            return False
        db = await self._conn()
        table = self._table_name(collection)
        async with db.execute(
            f'SELECT 1 FROM "{table}" WHERE id = ?', (entity_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    # --- Query Operations ---

    async def query(self, query: Query) -> list[dict[str, Any]]:
        if query.collection not in self._known_collections:
            return []
        db = await self._conn()
        table = self._table_name(query.collection)
        where_clause, params = self._build_where(query.filters)
        order_clause = self._build_order(query.sort)

        sql = f'SELECT id, data FROM "{table}"'
        if where_clause:
            sql += f" WHERE {where_clause}"
        if order_clause:
            sql += f" ORDER BY {order_clause}"
        if query.limit is not None:
            sql += " LIMIT ?"
            params.append(query.limit)
        if query.offset > 0:
            sql += " OFFSET ?"
            params.append(query.offset)

        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                data: dict[str, Any] = json.loads(row[1])
                data["_id"] = row[0]
                results.append(data)
            return results

    async def count(self, query: Query) -> int:
        if query.collection not in self._known_collections:
            return 0
        db = await self._conn()
        table = self._table_name(query.collection)
        where_clause, params = self._build_where(query.filters)

        sql = f'SELECT COUNT(*) FROM "{table}"'
        if where_clause:
            sql += f" WHERE {where_clause}"

        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0  # type: ignore[index]

    # --- Indexing ---

    async def ensure_index(self, index: IndexDefinition) -> None:
        await self._ensure_collection_table(index.collection)
        db = await self._conn()
        table = self._table_name(index.collection)
        name = index.name or f"idx_{index.collection}_{'_'.join(f.replace('.', '_') for f in index.fields)}"

        # Build index expression using json_extract
        expressions = [
            f"json_extract(data, '$.{field}')" for field in index.fields
        ]
        unique = "UNIQUE" if index.unique else ""
        expr_str = ", ".join(expressions)

        await db.execute(
            f'CREATE {unique} INDEX IF NOT EXISTS "{name}" ON "{table}" ({expr_str})'
        )
        await db.execute(
            "INSERT OR REPLACE INTO _indexes (name, collection, fields, is_unique) VALUES (?, ?, ?, ?)",
            (name, index.collection, json.dumps(index.fields), int(index.unique)),
        )
        await db.commit()

    async def list_indexes(self, collection: str) -> list[IndexDefinition]:
        db = await self._conn()
        async with db.execute(
            "SELECT name, collection, fields, is_unique FROM _indexes WHERE collection = ?",
            (collection,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                IndexDefinition(
                    collection=row[1],
                    fields=json.loads(row[2]),
                    name=row[0],
                    unique=bool(row[3]),
                )
                for row in rows
            ]

    # --- SQL Building Helpers ---

    @staticmethod
    def _json_path(field: str) -> str:
        """Convert dot-notation field to json_extract expression."""
        if field == "_id":
            return "id"
        return f"json_extract(data, '$.{field}')"

    def _build_where(self, filters: list[Filter]) -> tuple[str, list[Any]]:
        if not filters:
            return "", []

        clauses: list[str] = []
        params: list[Any] = []

        for f in filters:
            path = self._json_path(f.field)
            match f.op:
                case FilterOp.EQ:
                    clauses.append(f"{path} = ?")
                    params.append(self._serialize_value(f.value))
                case FilterOp.NEQ:
                    clauses.append(f"{path} != ?")
                    params.append(self._serialize_value(f.value))
                case FilterOp.GT:
                    clauses.append(f"{path} > ?")
                    params.append(self._serialize_value(f.value))
                case FilterOp.GTE:
                    clauses.append(f"{path} >= ?")
                    params.append(self._serialize_value(f.value))
                case FilterOp.LT:
                    clauses.append(f"{path} < ?")
                    params.append(self._serialize_value(f.value))
                case FilterOp.LTE:
                    clauses.append(f"{path} <= ?")
                    params.append(self._serialize_value(f.value))
                case FilterOp.IN:
                    placeholders = ", ".join("?" for _ in f.value)
                    clauses.append(f"{path} IN ({placeholders})")
                    params.extend(self._serialize_value(v) for v in f.value)
                case FilterOp.CONTAINS:
                    clauses.append(f"{path} LIKE ?")
                    params.append(f"%{f.value}%")
                case FilterOp.EXISTS:
                    if f.value:
                        clauses.append(f"{path} IS NOT NULL")
                    else:
                        clauses.append(f"{path} IS NULL")

        return " AND ".join(clauses), params

    def _build_order(self, sort: list["SortField"]) -> str:
        if not sort:
            return ""
        parts = []
        for s in sort:
            path = self._json_path(s.field)
            direction = "DESC" if s.descending else "ASC"
            parts.append(f"{path} {direction}")
        return ", ".join(parts)

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        """Serialize a value for SQLite parameter binding."""
        if isinstance(value, bool):
            return int(value)
        return value
