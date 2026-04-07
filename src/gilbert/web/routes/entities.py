"""Entity browser route — browse collections and entities in storage."""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.storage import Filter, FilterOp, Query, SortField, StorageBackend
from gilbert.web import templates
from gilbert.web.auth import require_role

router = APIRouter(prefix="/entities")

PAGE_SIZE = 50

# Filter operators exposed in the UI
_FILTER_OPS = [
    ("eq", "="),
    ("neq", "!="),
    ("gt", ">"),
    ("gte", ">="),
    ("lt", "<"),
    ("lte", "<="),
    ("contains", "contains"),
    ("exists", "exists"),
]


def _get_raw_storage(gilbert: Gilbert) -> StorageBackend | None:
    """Get the raw (un-namespaced) storage backend."""
    svc = gilbert.service_manager.get_by_capability("entity_storage")
    return getattr(svc, "raw_backend", None) if svc else None


def _group_by_namespace(
    collections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group collections by namespace prefix."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for col in collections:
        name = col["name"]
        parts = name.split(".", 1)
        if len(parts) == 2:
            ns = parts[0]
            if ns == "gilbert" and parts[1].startswith("plugin."):
                rest = parts[1][len("plugin."):]
                plugin_parts = rest.split(".", 1)
                if len(plugin_parts) == 2:
                    ns = f"gilbert.plugin.{plugin_parts[0]}"
                    col = {**col, "short_name": plugin_parts[1]}
                else:
                    col = {**col, "short_name": rest}
            else:
                col = {**col, "short_name": parts[1]}
        else:
            ns = "other"
            col = {**col, "short_name": name}
        groups.setdefault(ns, []).append(col)

    def ns_sort_key(ns: str) -> tuple[int, str]:
        if ns == "gilbert":
            return (0, ns)
        if ns.startswith("gilbert.plugin."):
            return (1, ns)
        return (2, ns)

    return [
        {"namespace": ns, "collections": sorted(cols, key=lambda c: c["short_name"])}
        for ns, cols in sorted(groups.items(), key=lambda x: ns_sort_key(x[0]))
    ]


async def _get_sortable_fields(
    storage: StorageBackend, collection: str,
) -> list[str]:
    """Build a list of sortable/filterable fields from indexes and FKs."""
    fields: set[str] = {"_id"}
    try:
        indexes = await storage.list_indexes(collection)
        for idx in indexes:
            for f in idx.fields:
                fields.add(f)
    except Exception:
        pass
    try:
        fks = await storage.list_foreign_keys(collection)
        for fk in fks:
            if fk.collection == collection:
                fields.add(fk.field)
    except Exception:
        pass
    return sorted(fields)


async def _get_fk_map(
    storage: StorageBackend, collection: str,
) -> dict[str, dict[str, str]]:
    """Build a map of field -> {ref_collection, ref_field} for FK fields in this collection."""
    fk_map: dict[str, dict[str, str]] = {}
    try:
        fks = await storage.list_foreign_keys(collection)
        for fk in fks:
            if fk.collection == collection:
                fk_map[fk.field] = {
                    "ref_collection": fk.ref_collection,
                    "ref_field": fk.ref_field,
                }
    except Exception:
        pass
    return fk_map


def _build_query_string(params: dict[str, str]) -> str:
    """Build a URL query string from non-empty params."""
    parts = [f"{k}={v}" for k, v in params.items() if v]
    return "&".join(parts)


@router.get("")
async def collections_list(request: Request, user: UserContext = Depends(require_role("admin"))) -> Any:
    """List all collections with entity counts, grouped by namespace."""
    gilbert: Gilbert = request.app.state.gilbert
    storage = _get_raw_storage(gilbert)

    collections: list[dict[str, Any]] = []
    if storage is not None:
        names = await storage.list_collections()
        for name in sorted(names):
            count = await storage.count(Query(collection=name))
            collections.append({"name": name, "count": count})

    groups = _group_by_namespace(collections)

    return templates.TemplateResponse(
        request, "entities.html", {"groups": groups, "total_collections": len(collections)}
    )


@router.get("/{collection}")
async def collection_detail(
    request: Request,
    collection: str,
    page: int = 1,
    sort: str = "",
    filter_field: str = "",
    filter_op: str = "eq",
    filter_value: str = "",
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    """Browse entities within a collection with sort and filter support."""
    gilbert: Gilbert = request.app.state.gilbert
    storage = _get_raw_storage(gilbert)

    entities: list[dict[str, Any]] = []
    total = 0
    sortable_fields: list[str] = ["_id"]
    fk_map: dict[str, dict[str, str]] = {}

    if storage is not None:
        sortable_fields = await _get_sortable_fields(storage, collection)
        fk_map = await _get_fk_map(storage, collection)

        # Build sort
        sort_fields: list[SortField] = []
        if sort:
            if sort.startswith("-"):
                sort_fields = [SortField(field=sort[1:], descending=True)]
            else:
                sort_fields = [SortField(field=sort, descending=False)]

        # Build filter
        filters: list[Filter] = []
        if filter_field and filter_op:
            try:
                op = FilterOp(filter_op)
                if op == FilterOp.EXISTS:
                    filters = [Filter(field=filter_field, op=op)]
                elif filter_value:
                    # Try to parse as number for comparison ops
                    value: Any = filter_value
                    if op in (FilterOp.GT, FilterOp.GTE, FilterOp.LT, FilterOp.LTE):
                        try:
                            value = float(filter_value)
                            if value == int(value):
                                value = int(value)
                        except ValueError:
                            pass
                    filters = [Filter(field=filter_field, op=op, value=value)]
            except ValueError:
                pass  # invalid op, ignore

        # Count with filter applied
        count_query = Query(collection=collection, filters=filters)
        total = await storage.count(count_query)

        offset = (page - 1) * PAGE_SIZE
        results = await storage.query(
            Query(
                collection=collection,
                filters=filters,
                sort=sort_fields,
                limit=PAGE_SIZE,
                offset=offset,
            )
        )
        entities = results

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    # Build base query params for pagination links (preserving sort/filter)
    base_params = {}
    if sort:
        base_params["sort"] = sort
    if filter_field and filter_op:
        base_params["filter_field"] = filter_field
        base_params["filter_op"] = filter_op
        if filter_value:
            base_params["filter_value"] = filter_value

    return templates.TemplateResponse(
        request,
        "collection.html",
        {
            "collection": collection,
            "entities": entities,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "page_size": PAGE_SIZE,
            "sort": sort,
            "sortable_fields": sortable_fields,
            "filter_field": filter_field,
            "filter_op": filter_op,
            "filter_value": filter_value,
            "filter_ops": _FILTER_OPS,
            "fk_map": fk_map,
            "base_params": base_params,
        },
    )


@router.get("/{collection}/{entity_id:path}")
async def entity_detail(
    request: Request, collection: str, entity_id: str,
    user: UserContext = Depends(require_role("admin")),
) -> Any:
    """View a single entity's full data with FK linking."""
    gilbert: Gilbert = request.app.state.gilbert
    storage = _get_raw_storage(gilbert)

    entity: dict[str, Any] | None = None
    fk_map: dict[str, dict[str, str]] = {}
    if storage is not None:
        entity = await storage.get(collection, entity_id)
        fk_map = await _get_fk_map(storage, collection)

    formatted = json.dumps(entity, indent=2, default=str) if entity else None

    return templates.TemplateResponse(
        request,
        "entity.html",
        {
            "collection": collection,
            "entity_id": entity_id,
            "entity": entity,
            "formatted": formatted,
            "fk_map": fk_map,
        },
    )
