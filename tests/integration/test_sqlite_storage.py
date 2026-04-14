"""Integration tests for SQLiteStorage — hits a real test database."""


from gilbert.interfaces.storage import Filter, FilterOp, IndexDefinition, Query, SortField
from gilbert.storage.sqlite import SQLiteStorage


# --- Entity CRUD ---


async def test_put_and_get(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("devices", "light-1", {
        "type": "light", "name": "Living Room", "integration": "lutron"
    })
    entity = await sqlite_storage.get("devices", "light-1")
    assert entity is not None
    assert entity["name"] == "Living Room"
    assert entity["type"] == "light"


async def test_get_not_found(sqlite_storage: SQLiteStorage) -> None:
    result = await sqlite_storage.get("devices", "nonexistent")
    assert result is None


async def test_get_from_unknown_collection(sqlite_storage: SQLiteStorage) -> None:
    result = await sqlite_storage.get("nonexistent_collection", "id-1")
    assert result is None


async def test_put_overwrites(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("devices", "light-1", {"name": "Old"})
    await sqlite_storage.put("devices", "light-1", {"name": "New"})
    entity = await sqlite_storage.get("devices", "light-1")
    assert entity is not None
    assert entity["name"] == "New"


async def test_delete(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("devices", "light-1", {"name": "Light"})
    await sqlite_storage.delete("devices", "light-1")
    assert await sqlite_storage.get("devices", "light-1") is None


async def test_delete_nonexistent(sqlite_storage: SQLiteStorage) -> None:
    # Should not raise
    await sqlite_storage.delete("devices", "nonexistent")


async def test_exists(sqlite_storage: SQLiteStorage) -> None:
    assert not await sqlite_storage.exists("devices", "light-1")
    await sqlite_storage.put("devices", "light-1", {"name": "Light"})
    assert await sqlite_storage.exists("devices", "light-1")


async def test_exists_unknown_collection(sqlite_storage: SQLiteStorage) -> None:
    assert not await sqlite_storage.exists("nonexistent_collection", "id-1")


# --- Query Operations ---


async def _seed_devices(storage: SQLiteStorage) -> None:
    await storage.put("devices", "light-1", {
        "type": "light", "name": "Living Room", "integration": "lutron", "brightness": 80
    })
    await storage.put("devices", "light-2", {
        "type": "light", "name": "Kitchen", "integration": "caseta", "brightness": 100
    })
    await storage.put("devices", "thermo-1", {
        "type": "thermostat", "name": "Hallway", "integration": "nest", "temp": 72
    })


async def test_query_all(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(collection="devices"))
    assert len(results) == 3


async def test_query_with_eq_filter(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[Filter(field="type", op=FilterOp.EQ, value="light")],
    ))
    assert len(results) == 2
    assert all(r["type"] == "light" for r in results)


async def test_query_with_gt_filter(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[Filter(field="brightness", op=FilterOp.GT, value=90)],
    ))
    assert len(results) == 1
    assert results[0]["name"] == "Kitchen"


async def test_query_with_in_filter(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[Filter(field="integration", op=FilterOp.IN, value=["lutron", "caseta"])],
    ))
    assert len(results) == 2


async def test_query_with_multiple_filters(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[
            Filter(field="type", op=FilterOp.EQ, value="light"),
            Filter(field="integration", op=FilterOp.EQ, value="lutron"),
        ],
    ))
    assert len(results) == 1
    assert results[0]["name"] == "Living Room"


async def test_query_with_contains_filter(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[Filter(field="name", op=FilterOp.CONTAINS, value="Room")],
    ))
    assert len(results) == 1
    assert results[0]["name"] == "Living Room"


async def test_query_with_exists_filter(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[Filter(field="brightness", op=FilterOp.EXISTS, value=True)],
    ))
    assert len(results) == 2  # light-1 and light-2


async def test_query_with_sort(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[Filter(field="type", op=FilterOp.EQ, value="light")],
        sort=[SortField(field="brightness", descending=True)],
    ))
    assert results[0]["brightness"] == 100
    assert results[1]["brightness"] == 80


async def test_query_with_limit_and_offset(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        sort=[SortField(field="name")],
        limit=2,
    ))
    assert len(results) == 2

    results_offset = await sqlite_storage.query(Query(
        collection="devices",
        sort=[SortField(field="name")],
        limit=2,
        offset=2,
    ))
    assert len(results_offset) == 1


async def test_query_empty_collection(sqlite_storage: SQLiteStorage) -> None:
    results = await sqlite_storage.query(Query(collection="nonexistent"))
    assert results == []


async def test_query_results_include_id(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("devices", "light-1", {"name": "Light"})
    results = await sqlite_storage.query(Query(collection="devices"))
    assert results[0]["_id"] == "light-1"


async def test_query_by_id_field(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    results = await sqlite_storage.query(Query(
        collection="devices",
        filters=[Filter(field="_id", op=FilterOp.EQ, value="light-1")],
    ))
    assert len(results) == 1
    assert results[0]["name"] == "Living Room"


async def test_count(sqlite_storage: SQLiteStorage) -> None:
    await _seed_devices(sqlite_storage)
    total = await sqlite_storage.count(Query(collection="devices"))
    assert total == 3

    lights = await sqlite_storage.count(Query(
        collection="devices",
        filters=[Filter(field="type", op=FilterOp.EQ, value="light")],
    ))
    assert lights == 2


async def test_count_empty(sqlite_storage: SQLiteStorage) -> None:
    count = await sqlite_storage.count(Query(collection="nonexistent"))
    assert count == 0


# --- Collection Management ---


async def test_list_collections(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("devices", "d1", {"name": "device"})
    await sqlite_storage.put("scenes", "s1", {"name": "scene"})
    collections = await sqlite_storage.list_collections()
    assert "devices" in collections
    assert "scenes" in collections


async def test_drop_collection(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("temp", "t1", {"data": "value"})
    assert await sqlite_storage.exists("temp", "t1")

    await sqlite_storage.drop_collection("temp")
    assert not await sqlite_storage.exists("temp", "t1")
    assert "temp" not in await sqlite_storage.list_collections()


# --- Indexing ---


async def test_ensure_index(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("devices", "d1", {"type": "light"})
    await sqlite_storage.ensure_index(IndexDefinition(
        collection="devices", fields=["type"]
    ))
    indexes = await sqlite_storage.list_indexes("devices")
    assert len(indexes) == 1
    assert indexes[0].fields == ["type"]


async def test_ensure_index_composite(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.put("devices", "d1", {"type": "light", "integration": "lutron"})
    await sqlite_storage.ensure_index(IndexDefinition(
        collection="devices", fields=["type", "integration"]
    ))
    indexes = await sqlite_storage.list_indexes("devices")
    assert len(indexes) == 1
    assert indexes[0].fields == ["type", "integration"]


async def test_ensure_index_unique(sqlite_storage: SQLiteStorage) -> None:
    await sqlite_storage.ensure_index(IndexDefinition(
        collection="users", fields=["email"], unique=True
    ))
    indexes = await sqlite_storage.list_indexes("users")
    assert len(indexes) == 1
    assert indexes[0].unique is True


async def test_ensure_index_idempotent(sqlite_storage: SQLiteStorage) -> None:
    idx = IndexDefinition(collection="devices", fields=["type"])
    await sqlite_storage.ensure_index(idx)
    await sqlite_storage.ensure_index(idx)  # should not raise
    indexes = await sqlite_storage.list_indexes("devices")
    assert len(indexes) == 1
