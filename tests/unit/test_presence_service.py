"""Tests for PresenceService — focus on the new history + tool surface.

The existing presence service has been around for a while but had no
direct unit coverage; these tests cover the Phase-A additions:

- ``_record_detection_history`` writes one row per (user_id, date, source)
  and rolls up first_seen / last_seen / observation_count across polls.
- ``_prune_old_history`` deletes rows past the retention horizon.
- ``get_detection_history`` returns rows in a stable shape, filtered by
  optional inclusive ``since`` / ``until`` bounds.
- ``presence_history`` tool resolves names via the user service,
  rolls up multi-source detections per day, and surfaces helpful
  errors for the unresolvable / empty-input cases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.core.services.presence import (
    PresenceService,
    _DEFAULT_HISTORY_RETENTION_DAYS,
)
from gilbert.interfaces.presence import (
    PresenceDetection,
    PresenceHistoryProvider,
    PresenceState,
    UserPresence,
)
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.users import NameMatch


class _MemoryStorage:
    """Just enough of a StorageBackend to drive the detection-history tests.

    Supports ``get`` / ``put`` / ``delete`` and a minimal ``query``
    that handles the EQ/GTE/LTE/LT filters and ASC sort the presence
    service actually uses. Anything else raises so we notice if a code
    path drifts and starts depending on a richer feature.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = value

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def query(self, q: Any) -> list[dict[str, Any]]:
        from gilbert.interfaces.storage import FilterOp

        coll = self._data.get(q.collection, {})
        rows: list[dict[str, Any]] = []
        for key, val in coll.items():
            keep = True
            for f in q.filters or []:
                v = val.get(f.field)
                if f.op == FilterOp.EQ:
                    keep &= v == f.value
                elif f.op == FilterOp.GTE:
                    keep &= v is not None and v >= f.value
                elif f.op == FilterOp.LTE:
                    keep &= v is not None and v <= f.value
                elif f.op == FilterOp.LT:
                    keep &= v is not None and v < f.value
                else:
                    raise NotImplementedError(f"Memory storage doesn't handle {f.op}")
                if not keep:
                    break
            if keep:
                row = dict(val)
                row["_id"] = key
                rows.append(row)
        for sort_field in reversed(list(q.sort or [])):
            rows.sort(key=lambda r: r.get(sort_field.field, ""))
        return rows


class _FakeUserSvc:
    """Concrete UserManagementProvider so isinstance(...) narrows."""

    def __init__(self, users: dict[str, dict[str, Any]], match: NameMatch | None = None) -> None:
        self._users = users
        self._match = match

    @property
    def allow_user_creation(self) -> bool:
        return False

    @property
    def backend(self) -> Any:
        class _B:
            async def get_user(_self, uid: str) -> dict[str, Any] | None:
                return self._users.get(uid)

        return _B()

    async def list_users(self) -> list[dict[str, Any]]:
        return list(self._users.values())

    async def resolve_user_id_by_name(self, name: str) -> NameMatch | None:
        return self._match


class _Resolver(ServiceResolver):
    def __init__(self, caps: dict[str, Any]) -> None:
        self._caps = caps

    def get_capability(self, capability: str) -> Any:
        return self._caps.get(capability)

    def require_capability(self, capability: str) -> Any:
        if capability in self._caps:
            return self._caps[capability]
        raise LookupError(capability)

    def get_all(self, capability: str) -> list[Any]:
        v = self._caps.get(capability)
        return [v] if v else []


def _make_svc(*, storage: Any, resolver: Any, retention_days: int | None = None) -> PresenceService:
    svc = PresenceService()
    svc._storage = storage
    svc._resolver = resolver
    if retention_days is not None:
        svc._history_retention_days = retention_days
    return svc


# --- _record_detection_history --------------------------------------


@pytest.mark.asyncio
async def test_record_detection_history_creates_row_per_source() -> None:
    """A poll that sees the same user via two sources writes two rows;
    a third poll bumps observation_count and last_seen but preserves
    first_seen and creates no duplicate rows."""
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))

    # Two sources for the same user_id on the same day.
    await svc._record_detection_history(
        {
            "u1": UserPresence(user_id="u1", state=PresenceState.PRESENT, source="unifi:protect"),
        },
    )
    await svc._record_detection_history(
        {
            "u1": UserPresence(user_id="u1", state=PresenceState.PRESENT, source="unifi:network"),
        },
    )

    rows = list(storage._data.get("presence_detections", {}).values())
    assert len(rows) == 2
    by_source = {r["source"]: r for r in rows}
    assert by_source["unifi:protect"]["observation_count"] == 1
    assert by_source["unifi:network"]["observation_count"] == 1

    # Third poll on the same source: count bumps, first_seen stays.
    first_protect_seen = by_source["unifi:protect"]["first_seen"]
    await svc._record_detection_history(
        {"u1": UserPresence(user_id="u1", state=PresenceState.PRESENT, source="unifi:protect")},
    )
    by_source = {
        r["source"]: r for r in storage._data["presence_detections"].values()
    }
    assert by_source["unifi:protect"]["observation_count"] == 2
    assert by_source["unifi:protect"]["first_seen"] == first_protect_seen


@pytest.mark.asyncio
async def test_record_detection_history_skips_away_and_unknown() -> None:
    """We only record present/nearby — away/unknown polls don't fill the
    table with no-op rows."""
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))

    await svc._record_detection_history(
        {
            "u_away": UserPresence(user_id="u_away", state=PresenceState.AWAY, source="x"),
            "u_unknown": UserPresence(user_id="u_unknown", state=PresenceState.UNKNOWN, source="x"),
            "u_here": UserPresence(user_id="u_here", state=PresenceState.PRESENT, source="x"),
            "u_nearby": UserPresence(user_id="u_nearby", state=PresenceState.NEARBY, source="x"),
        }
    )

    rows = storage._data.get("presence_detections", {})
    ids = {r["user_id"] for r in rows.values()}
    assert ids == {"u_here", "u_nearby"}


@pytest.mark.asyncio
async def test_record_detection_history_noops_without_storage() -> None:
    """No storage means no rows — must not crash."""
    svc = _make_svc(storage=None, resolver=_Resolver({}))
    await svc._record_detection_history(
        {"u1": UserPresence(user_id="u1", state=PresenceState.PRESENT, source="x")}
    )  # no assertion needed — just shouldn't raise.


# --- _prune_old_history ---------------------------------------------


@pytest.mark.asyncio
async def test_prune_old_history_drops_rows_past_retention() -> None:
    """Rows whose date is older than retention go away; recent rows stay."""
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}), retention_days=7)

    today = datetime.now(UTC).date()
    old_date = (today - timedelta(days=10)).isoformat()
    fresh_date = (today - timedelta(days=3)).isoformat()
    very_recent = today.isoformat()
    for i, d in enumerate([old_date, fresh_date, very_recent]):
        await storage.put(
            "presence_detections",
            f"u1|{d}|src",
            {
                "user_id": "u1",
                "date": d,
                "source": "src",
                "first_seen": "",
                "last_seen": "",
                "observation_count": 1,
            },
        )

    await svc._prune_old_history()

    remaining = {r["date"] for r in storage._data["presence_detections"].values()}
    assert old_date not in remaining
    assert fresh_date in remaining
    assert very_recent in remaining


@pytest.mark.asyncio
async def test_prune_old_history_disabled_when_retention_zero() -> None:
    """Retention 0 = keep forever — sweep is a no-op."""
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}), retention_days=0)

    very_old = (datetime.now(UTC).date() - timedelta(days=365)).isoformat()
    await storage.put(
        "presence_detections",
        f"u1|{very_old}|src",
        {
            "user_id": "u1",
            "date": very_old,
            "source": "src",
            "first_seen": "",
            "last_seen": "",
            "observation_count": 1,
        },
    )
    await svc._prune_old_history()
    assert len(storage._data["presence_detections"]) == 1


# --- get_detection_history -----------------------------------------


@pytest.mark.asyncio
async def test_get_detection_history_filters_by_window() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))
    today = datetime.now(UTC).date()
    for delta in (0, 1, 4, 10):
        d = (today - timedelta(days=delta)).isoformat()
        await storage.put(
            "presence_detections",
            f"u1|{d}|src",
            {
                "user_id": "u1",
                "date": d,
                "source": "src",
                "first_seen": "",
                "last_seen": "",
                "observation_count": 1,
            },
        )
    # Window: yesterday → today
    since = (today - timedelta(days=1)).isoformat()
    until = today.isoformat()
    rows = await svc.get_detection_history("u1", since=since, until=until)
    assert [r.date for r in rows] == [since, until]


@pytest.mark.asyncio
async def test_get_detection_history_returns_empty_for_unknown_user() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))
    assert await svc.get_detection_history("nobody") == []


@pytest.mark.asyncio
async def test_get_detection_history_returns_typed_dataclasses() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))
    today = datetime.now(UTC).date().isoformat()
    await storage.put(
        "presence_detections",
        f"u1|{today}|src",
        {
            "user_id": "u1",
            "date": today,
            "source": "src",
            "first_seen": "2026-01-01T08:00:00+00:00",
            "last_seen": "2026-01-01T17:00:00+00:00",
            "observation_count": 12,
        },
    )
    rows = await svc.get_detection_history("u1")
    assert len(rows) == 1
    assert isinstance(rows[0], PresenceDetection)
    assert rows[0].observation_count == 12
    assert rows[0].first_seen == "2026-01-01T08:00:00+00:00"


def test_service_satisfies_presence_history_provider_protocol() -> None:
    """The service is the canonical PresenceHistoryProvider — callers
    rely on isinstance to type-narrow before calling get_detection_history."""
    assert isinstance(PresenceService(), PresenceHistoryProvider)


# --- presence_history tool -----------------------------------------


@pytest.mark.asyncio
async def test_tool_resolves_free_form_name_via_user_service() -> None:
    """Tool accepts ``Brian`` and resolves it via the user service's
    resolve_user_id_by_name (just like /greet does)."""
    storage = _MemoryStorage()
    users = _FakeUserSvc(
        users={"u_brian": {"user_id": "u_brian", "display_name": "Brian", "email": "b@x"}},
        match=NameMatch(user_id="u_brian", confidence=0.8),
    )
    resolver = _Resolver({"users": users})
    svc = _make_svc(storage=storage, resolver=resolver)

    today = datetime.now(UTC).date().isoformat()
    await storage.put(
        "presence_detections",
        f"u_brian|{today}|unifi:network",
        {
            "user_id": "u_brian",
            "date": today,
            "source": "unifi:network",
            "first_seen": "2026-05-14T07:30:00+00:00",
            "last_seen": "2026-05-14T17:45:00+00:00",
            "observation_count": 24,
        },
    )

    import json

    result = json.loads(
        await svc.execute_tool("presence_history", {"name_or_user_id": "Brian", "days": 7})
    )
    assert result["user_id"] == "u_brian"
    assert len(result["days"]) == 1
    assert result["days"][0]["observation_count"] == 24
    assert result["days"][0]["by_source"]["unifi:network"]["observation_count"] == 24


@pytest.mark.asyncio
async def test_tool_accepts_direct_user_id_without_name_resolver() -> None:
    """If ``name_or_user_id`` is itself a known user_id, the tool short-
    circuits the name resolver — no ambiguity risk."""
    users = _FakeUserSvc(
        users={"u_direct": {"user_id": "u_direct", "display_name": "X"}},
        # match=None to prove the resolver wasn't consulted
        match=None,
    )
    resolver = _Resolver({"users": users})
    svc = _make_svc(storage=_MemoryStorage(), resolver=resolver)

    import json

    result = json.loads(
        await svc.execute_tool("presence_history", {"name_or_user_id": "u_direct"})
    )
    assert result["user_id"] == "u_direct"
    assert result["days"] == []


@pytest.mark.asyncio
async def test_tool_reports_unresolvable_name() -> None:
    users = _FakeUserSvc(users={}, match=None)
    resolver = _Resolver({"users": users})
    svc = _make_svc(storage=_MemoryStorage(), resolver=resolver)

    import json

    result = json.loads(
        await svc.execute_tool("presence_history", {"name_or_user_id": "ghost"})
    )
    assert "error" in result
    assert "ghost" in result["error"]


@pytest.mark.asyncio
async def test_tool_rejects_empty_input_and_nonpositive_days() -> None:
    import json

    svc = _make_svc(storage=_MemoryStorage(), resolver=_Resolver({}))
    out_empty = json.loads(await svc.execute_tool("presence_history", {"name_or_user_id": ""}))
    assert "required" in out_empty["error"]
    out_zero = json.loads(
        await svc.execute_tool(
            "presence_history", {"name_or_user_id": "x", "days": 0}
        )
    )
    assert "days" in out_zero["error"]


@pytest.mark.asyncio
async def test_tool_rolls_up_multi_source_days() -> None:
    """A user seen via two sources on the same day appears as one
    aggregated entry in the tool output, with by_source detail preserved."""
    storage = _MemoryStorage()
    users = _FakeUserSvc(
        users={"u1": {"user_id": "u1", "display_name": "U"}},
        match=NameMatch(user_id="u1", confidence=1.0),
    )
    resolver = _Resolver({"users": users})
    svc = _make_svc(storage=storage, resolver=resolver)

    today = datetime.now(UTC).date().isoformat()
    for src, count in (("unifi:network", 5), ("unifi:protect", 9)):
        await storage.put(
            "presence_detections",
            f"u1|{today}|{src}",
            {
                "user_id": "u1",
                "date": today,
                "source": src,
                "first_seen": "2026-05-14T07:00:00+00:00",
                "last_seen": "2026-05-14T18:00:00+00:00",
                "observation_count": count,
            },
        )

    import json

    result = json.loads(
        await svc.execute_tool("presence_history", {"name_or_user_id": "U"})
    )
    assert len(result["days"]) == 1
    day = result["days"][0]
    assert day["observation_count"] == 14
    assert set(day["by_source"].keys()) == {"unifi:network", "unifi:protect"}


# --- config wiring -------------------------------------------------


def test_default_retention_is_30_days() -> None:
    """The bundled default for retention is 30 — change deliberately, not
    accidentally."""
    assert _DEFAULT_HISTORY_RETENTION_DAYS == 30
    svc = PresenceService()
    assert svc._history_retention_days == _DEFAULT_HISTORY_RETENTION_DAYS


def test_config_params_includes_retention_and_timezone() -> None:
    svc = PresenceService()
    keys = {p.key for p in svc.config_params()}
    assert "history_retention_days" in keys
    assert "timezone" in keys


# --- Phase B: observations + mapping API ---------------------------


from gilbert.interfaces.presence import PresenceObservation  # noqa: E402


@pytest.mark.asyncio
async def test_upsert_observations_preserves_first_seen_and_mapping() -> None:
    """A first sighting writes first_seen=now; a second poll bumps
    last_seen and leaves first_seen + mapped_user_id alone."""
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))

    await svc._upsert_observations(
        [
            PresenceObservation(
                backend="unifi:protect",
                thing_id="Brian",
                label="Brian D",
                kind="face",
                first_seen="2026-05-14T08:00:00+00:00",
                last_seen="2026-05-14T08:00:00+00:00",
            ),
        ]
    )
    # Admin maps it.
    await svc.map_thing("unifi:protect", "Brian", "usr_brian")
    # Second poll comes in.
    await svc._upsert_observations(
        [
            PresenceObservation(
                backend="unifi:protect",
                thing_id="Brian",
                label="Brian D",
                kind="face",
                first_seen="2026-05-14T09:00:00+00:00",
                last_seen="2026-05-14T09:00:00+00:00",
            ),
        ]
    )

    row = await storage.get("presence_observations", "unifi:protect:Brian")
    assert row is not None
    assert row["first_seen"] == "2026-05-14T08:00:00+00:00"
    assert row["last_seen"] == "2026-05-14T09:00:00+00:00"
    assert row["mapped_user_id"] == "usr_brian"


@pytest.mark.asyncio
async def test_list_observations_filters_by_mapped_state() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))
    await svc._upsert_observations(
        [
            PresenceObservation(backend="unifi:protect", thing_id="A", kind="face"),
            PresenceObservation(backend="unifi:network", thing_id="B", kind="wifi"),
        ]
    )
    await svc.map_thing("unifi:protect", "A", "usr_a")

    all_rows = await svc.list_observations()
    mapped = await svc.list_observations(mapped=True)
    unmapped = await svc.list_observations(mapped=False)

    assert {r["thing_id"] for r in all_rows} == {"A", "B"}
    assert {r["thing_id"] for r in mapped} == {"A"}
    assert {r["thing_id"] for r in unmapped} == {"B"}


@pytest.mark.asyncio
async def test_map_thing_returns_none_for_unknown_observation() -> None:
    svc = _make_svc(storage=_MemoryStorage(), resolver=_Resolver({}))
    assert await svc.map_thing("unifi:protect", "ghost", "usr_x") is None


@pytest.mark.asyncio
async def test_relabel_thing_does_not_change_mapping() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(storage=storage, resolver=_Resolver({}))
    await svc._upsert_observations(
        [PresenceObservation(backend="unifi:protect", thing_id="X", label="X", kind="face")]
    )
    await svc.map_thing("unifi:protect", "X", "usr_x")
    relabeled = await svc.relabel_thing("unifi:protect", "X", "Mr. X")
    assert relabeled is not None
    assert relabeled["label"] == "Mr. X"
    assert relabeled["mapped_user_id"] == "usr_x"


@pytest.mark.asyncio
async def test_map_thing_pushes_full_mapping_set_to_backend() -> None:
    """After the admin maps an observation, the backend's
    ``apply_thing_mappings`` hook is invoked with the full current
    mapping set (not just the delta), so the backend can swap state
    atomically rather than reconcile patches."""
    storage = _MemoryStorage()

    class _RecordingBackend:
        def __init__(self) -> None:
            self.applied: list[dict[str, str]] = []

        async def apply_thing_mappings(self, mappings: dict[str, str]) -> None:
            self.applied.append(dict(mappings))

    svc = _make_svc(storage=storage, resolver=_Resolver({}))
    backend = _RecordingBackend()
    svc._backend = backend  # type: ignore[assignment]

    await svc._upsert_observations(
        [
            PresenceObservation(backend="unifi:protect", thing_id="A", kind="face"),
            PresenceObservation(backend="unifi:network", thing_id="B", kind="wifi"),
        ]
    )
    await svc.map_thing("unifi:protect", "A", "usr_a")

    assert backend.applied, "backend should have received apply_thing_mappings"
    snap = backend.applied[-1]
    # Both observation row ids are present; mapped one carries the
    # user_id, the other carries an empty string.
    assert snap["unifi:protect:A"] == "usr_a"
    assert snap["unifi:network:B"] == ""


# --- WS handler tests ----------------------------------------------


def _admin_acl() -> Any:
    """Stub AccessControlProvider where admin == level 0.

    The Protocol declares several methods beyond ``get_role_level`` —
    we stub them all so the runtime_checkable isinstance check passes,
    even though only ``get_role_level`` is exercised by the WS gate.
    """
    from gilbert.interfaces.auth import AccessControlProvider

    class _Acl:
        def get_role_level(self, role: str) -> int:
            return 0 if role == "admin" else 100

        def get_effective_level(self, user_ctx: Any) -> int:
            return 0

        def resolve_rpc_level(self, frame_type: str) -> int:
            return 100

        def get_rpc_override_level(self, frame_type: str) -> int | None:
            return None

        def check_collection_read(self, user_ctx: Any, collection: str) -> bool:
            return True

        def check_collection_write(self, user_ctx: Any, collection: str) -> bool:
            return True

    acl = _Acl()
    assert isinstance(acl, AccessControlProvider)
    return acl


class _Conn:
    def __init__(self, user_level: int) -> None:
        self.user_level = user_level


@pytest.mark.asyncio
async def test_ws_things_list_blocked_for_non_admin() -> None:
    svc = _make_svc(
        storage=_MemoryStorage(),
        resolver=_Resolver({"access_control": _admin_acl()}),
    )
    result = await svc._ws_things_list(_Conn(user_level=100), {"id": "1"})
    assert result is not None
    assert result["type"] == "gilbert.error"
    assert result["code"] == 403


@pytest.mark.asyncio
async def test_ws_things_list_returns_rows_for_admin() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(
        storage=storage,
        resolver=_Resolver({"access_control": _admin_acl()}),
    )
    await svc._upsert_observations(
        [PresenceObservation(backend="unifi:protect", thing_id="X", kind="face")]
    )
    result = await svc._ws_things_list(_Conn(user_level=0), {"id": "1"})
    assert result is not None
    assert result["type"] == "presence.things.list.result"
    assert len(result["things"]) == 1
    assert result["things"][0]["thing_id"] == "X"


@pytest.mark.asyncio
async def test_ws_things_list_filters_mapped() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(
        storage=storage,
        resolver=_Resolver({"access_control": _admin_acl()}),
    )
    await svc._upsert_observations(
        [
            PresenceObservation(backend="unifi:protect", thing_id="A", kind="face"),
            PresenceObservation(backend="unifi:network", thing_id="B", kind="wifi"),
        ]
    )
    await svc.map_thing("unifi:protect", "A", "usr_a")
    result = await svc._ws_things_list(
        _Conn(user_level=0), {"id": "1", "mapped": False}
    )
    assert result is not None
    things = result["things"]
    assert len(things) == 1
    assert things[0]["thing_id"] == "B"


@pytest.mark.asyncio
async def test_ws_things_map_validates_inputs_and_404() -> None:
    svc = _make_svc(
        storage=_MemoryStorage(),
        resolver=_Resolver({"access_control": _admin_acl()}),
    )
    # Missing required fields.
    bad = await svc._ws_things_map(_Conn(user_level=0), {"id": "1"})
    assert bad is not None
    assert bad["code"] == 400
    # Unknown thing.
    nope = await svc._ws_things_map(
        _Conn(user_level=0),
        {
            "id": "2",
            "backend": "unifi:protect",
            "thing_id": "ghost",
            "user_id": "usr_x",
        },
    )
    assert nope is not None
    assert nope["code"] == 404


@pytest.mark.asyncio
async def test_ws_things_unmap_round_trip() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(
        storage=storage,
        resolver=_Resolver({"access_control": _admin_acl()}),
    )
    await svc._upsert_observations(
        [PresenceObservation(backend="unifi:protect", thing_id="X", kind="face")]
    )
    await svc.map_thing("unifi:protect", "X", "usr_x")

    result = await svc._ws_things_unmap(
        _Conn(user_level=0),
        {"id": "1", "backend": "unifi:protect", "thing_id": "X"},
    )
    assert result is not None
    assert result["type"] == "presence.things.unmap.result"
    assert result["thing"]["mapped_user_id"] == ""


@pytest.mark.asyncio
async def test_ws_things_relabel_updates_label() -> None:
    storage = _MemoryStorage()
    svc = _make_svc(
        storage=storage,
        resolver=_Resolver({"access_control": _admin_acl()}),
    )
    await svc._upsert_observations(
        [PresenceObservation(backend="unifi:protect", thing_id="X", label="X", kind="face")]
    )
    result = await svc._ws_things_relabel(
        _Conn(user_level=0),
        {"id": "1", "backend": "unifi:protect", "thing_id": "X", "label": "Marvelous X"},
    )
    assert result is not None
    assert result["thing"]["label"] == "Marvelous X"
