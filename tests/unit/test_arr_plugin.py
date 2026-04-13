"""Tests for the arr plugin (Radarr + Sonarr).

The arr plugin lives under ``plugins/arr/``, which is gitignored per the
installation-local plugin convention. These tests run only when the
plugin is present on disk — on a fresh checkout or CI without the
plugin they're skipped at collection time rather than crashing at import.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

# ── Register the plugin package ──────────────────────────────────────
# The arr plugin uses relative imports (``from .arr_client import ...``),
# so pytest needs to see the plugin directory as a proper Python package
# before the service modules can be imported.

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "arr"
_PKG_NAME = "gilbert_plugin_arr"

if not _PLUGIN_DIR.is_dir():
    pytest.skip(
        f"arr plugin not installed at {_PLUGIN_DIR}",
        allow_module_level=True,
    )

if _PKG_NAME not in sys.modules:
    _pkg = ModuleType(_PKG_NAME)
    _pkg.__path__ = [str(_PLUGIN_DIR)]
    _pkg.__package__ = _PKG_NAME
    sys.modules[_PKG_NAME] = _pkg

    for _mod_name in ("arr_client", "radarr_service", "sonarr_service", "plugin"):
        _spec = importlib.util.spec_from_file_location(
            f"{_PKG_NAME}.{_mod_name}",
            _PLUGIN_DIR / f"{_mod_name}.py",
            submodule_search_locations=[],
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_PKG_NAME}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(_pkg, _mod_name, _mod)

# Now the plugin modules are importable
from gilbert_plugin_arr.plugin import ArrPlugin, create_plugin  # noqa: E402
from gilbert_plugin_arr.radarr_service import RadarrService  # noqa: E402
from gilbert_plugin_arr.sonarr_service import SonarrService  # noqa: E402

from gilbert.interfaces.ui import ToolOutput, UIBlock  # noqa: E402

# ── Fakes ────────────────────────────────────────────────────────────


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with the given status code."""
    request = httpx.Request("GET", "http://example/fake")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=response,
    )


class FakeArrClient:
    """In-memory fake ArrClient that records requests and returns canned responses."""

    def __init__(self) -> None:
        self.name = "fake"
        self.available = True
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.responses: dict[tuple[str, str], Any] = {}
        self.errors: dict[tuple[str, str], Exception] = {}

    def set(self, method: str, path: str, data: Any) -> None:
        self.responses[(method, path)] = data

    def set_error(self, method: str, path: str, exc: Exception) -> None:
        self.errors[(method, path)] = exc

    def _dispatch(
        self, method: str, path: str, payload: dict[str, Any] | None,
    ) -> Any:
        self.calls.append((method, path, payload))
        err = self.errors.get((method, path))
        if err is not None:
            raise err
        return self.responses.get((method, path), [] if method == "GET" else {})

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._dispatch("GET", path, params)

    async def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self._dispatch("POST", path, data)

    async def put(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self._dispatch("PUT", path, data)

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._dispatch("DELETE", path, params)

    async def close(self) -> None:
        pass


@pytest.fixture
def radarr() -> RadarrService:
    svc = RadarrService()
    svc._enabled = True
    svc._client = FakeArrClient()  # type: ignore[assignment]
    return svc


@pytest.fixture
def sonarr() -> SonarrService:
    svc = SonarrService()
    svc._enabled = True
    svc._client = FakeArrClient()  # type: ignore[assignment]
    return svc


# ── Plugin metadata ──────────────────────────────────────────────────


def test_plugin_entrypoint_returns_plugin_instance() -> None:
    plugin = create_plugin()
    assert isinstance(plugin, ArrPlugin)
    meta = plugin.metadata()
    assert meta.name == "arr"
    assert "radarr" in meta.provides
    assert "sonarr" in meta.provides


def test_radarr_service_info_declares_ai_tools() -> None:
    svc = RadarrService()
    info = svc.service_info()
    assert info.name == "radarr"
    assert "ai_tools" in info.capabilities
    assert info.toggleable


def test_sonarr_service_info_declares_ai_tools() -> None:
    svc = SonarrService()
    info = svc.service_info()
    assert info.name == "sonarr"
    assert "ai_tools" in info.capabilities
    assert info.toggleable


# ── Tool visibility ──────────────────────────────────────────────────


def test_disabled_radarr_returns_no_tools() -> None:
    assert RadarrService().get_tools() == []


def test_disabled_sonarr_returns_no_tools() -> None:
    assert SonarrService().get_tools() == []


def test_enabled_radarr_exposes_expected_tools(radarr: RadarrService) -> None:
    names = {t.name for t in radarr.get_tools()}
    expected = {
        "radarr_search", "radarr_find", "radarr_list", "radarr_details",
        "radarr_upcoming", "radarr_queue", "radarr_recent", "radarr_profiles",
        "radarr_add", "radarr_remove", "radarr_grab",
    }
    assert expected <= names


def test_enabled_sonarr_exposes_expected_tools(sonarr: SonarrService) -> None:
    names = {t.name for t in sonarr.get_tools()}
    expected = {
        "sonarr_search", "sonarr_find", "sonarr_list", "sonarr_details",
        "sonarr_episodes", "sonarr_upcoming", "sonarr_queue", "sonarr_recent",
        "sonarr_profiles", "sonarr_add", "sonarr_remove", "sonarr_grab",
    }
    assert expected <= names


def test_radarr_find_is_user_level(radarr: RadarrService) -> None:
    by_name = {t.name: t for t in radarr.get_tools()}
    assert by_name["radarr_find"].required_role == "user"
    assert by_name["radarr_find"].slash_command == "find"


def test_sonarr_find_is_user_level(sonarr: SonarrService) -> None:
    by_name = {t.name: t for t in sonarr.get_tools()}
    assert by_name["sonarr_find"].required_role == "user"
    assert by_name["sonarr_find"].slash_command == "find"


def test_radarr_write_tools_require_admin(radarr: RadarrService) -> None:
    by_name = {t.name: t for t in radarr.get_tools()}
    assert by_name["radarr_add"].required_role == "admin"
    assert by_name["radarr_remove"].required_role == "admin"
    assert by_name["radarr_grab"].required_role == "admin"
    assert by_name["radarr_search"].required_role == "user"


def test_sonarr_write_tools_require_admin(sonarr: SonarrService) -> None:
    by_name = {t.name: t for t in sonarr.get_tools()}
    assert by_name["sonarr_add"].required_role == "admin"
    assert by_name["sonarr_remove"].required_role == "admin"
    assert by_name["sonarr_grab"].required_role == "admin"
    assert by_name["sonarr_search"].required_role == "user"


def test_radarr_tools_have_slash_commands(radarr: RadarrService) -> None:
    for tool in radarr.get_tools():
        assert tool.slash_command, f"{tool.name} missing slash_command"
        assert tool.slash_help, f"{tool.name} missing slash_help"


def test_sonarr_tools_have_slash_commands(sonarr: SonarrService) -> None:
    for tool in sonarr.get_tools():
        assert tool.slash_command, f"{tool.name} missing slash_command"
        assert tool.slash_help, f"{tool.name} missing slash_help"


def test_services_declare_plugin_slash_namespaces() -> None:
    assert RadarrService.slash_namespace == "radarr"
    assert SonarrService.slash_namespace == "sonarr"


# ── Radarr behaviour ─────────────────────────────────────────────────


async def test_radarr_disabled_returns_error_message() -> None:
    svc = RadarrService()
    result = await svc.execute_tool("radarr_list", {})
    assert "not configured" in result.lower()


async def test_radarr_search_formats_results(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie/lookup",
        [
            {
                "title": "Inception", "year": 2010, "runtime": 148,
                "tmdbId": 27205, "id": 0, "overview": "A thief who steals corporate secrets...",
                "images": [],
            },
        ],
    )
    result = await radarr.execute_tool("radarr_search", {"query": "inception"})
    assert "Inception" in result
    assert "2010" in result
    assert "tmdb_id: 27205" in result
    assert "not in library" in result


async def test_radarr_search_requires_query(radarr: RadarrService) -> None:
    result = await radarr.execute_tool("radarr_search", {})
    assert "specify a movie" in result.lower()


async def test_radarr_list_skips_unmonitored(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie",
        [
            {"title": "Monitored", "year": 2001, "hasFile": True, "monitored": True, "id": 1},
            {"title": "Skipped", "year": 2002, "hasFile": False, "monitored": False, "id": 2},
        ],
    )
    result = await radarr.execute_tool("radarr_list", {})
    assert "Monitored" in result
    assert "Skipped" not in result


async def test_radarr_details_resolves_by_name(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie",
        [{"id": 42, "title": "The Matrix"}],
    )
    client.set(
        "GET", "/movie/42",
        {
            "id": 42, "title": "The Matrix", "year": 1999,
            "hasFile": True, "runtime": 136, "genres": ["Action"],
            "monitored": True, "images": [],
            "movieFile": {
                "quality": {"quality": {"name": "1080p"}},
                "size": 5_368_709_120,
            },
        },
    )
    result = await radarr.execute_tool("radarr_details", {"movie": "matrix"})
    assert "The Matrix" in result
    assert "downloaded" in result
    assert "1080p" in result


async def test_radarr_details_missing_returns_friendly_message(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set("GET", "/movie", [])
    result = await radarr.execute_tool("radarr_details", {"movie": "nope"})
    assert "not found" in result.lower()


async def test_radarr_add_happy_path(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie/lookup",
        [{
            "title": "Dune", "year": 2021, "tmdbId": 438631, "id": 0,
            "images": [],
        }],
    )
    client.set("GET", "/rootfolder", [{"path": "/movies"}])
    client.set(
        "GET", "/qualityprofile",
        [{"id": 1, "name": "HD"}, {"id": 2, "name": "Ultra-HD"}],
    )
    client.set("POST", "/movie", {"title": "Dune"})

    result = await radarr.execute_tool(
        "radarr_add",
        {"tmdb_id": 438631, "quality_profile": "ultra"},
    )
    assert "Added" in result
    assert "Dune" in result

    post_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/movie"]
    assert len(post_calls) == 1
    payload = post_calls[0][2]
    assert payload is not None
    assert payload["qualityProfileId"] == 2  # Ultra-HD matched by partial name
    assert payload["rootFolderPath"] == "/movies"
    assert payload["monitored"] is True
    assert payload["addOptions"]["searchForMovie"] is True


async def test_radarr_add_rejects_missing_tmdb_id(radarr: RadarrService) -> None:
    result = await radarr.execute_tool("radarr_add", {})
    assert "tmdb_id" in result.lower()


async def test_radarr_add_rejects_already_in_library(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie/lookup",
        [{"title": "Dune", "tmdbId": 438631, "id": 5, "images": []}],
    )
    result = await radarr.execute_tool("radarr_add", {"tmdb_id": 438631})
    assert "already in your library" in result


async def test_radarr_remove_deletes_files(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    result = await radarr.execute_tool("radarr_remove", {"movie": "7"})
    assert "7" in result
    delete_calls = [c for c in client.calls if c[0] == "DELETE"]
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == "/movie/7"
    assert delete_calls[0][2] == {"deleteFiles": "true"}


async def test_radarr_grab_triggers_command(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    await radarr.execute_tool("radarr_grab", {"movie": 3})
    post_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/command"]
    assert len(post_calls) == 1
    assert post_calls[0][2] == {"name": "MoviesSearch", "movieIds": [3]}


def test_radarr_resolve_profile_id_prefers_exact_id() -> None:
    profiles = [{"id": 1, "name": "SD"}, {"id": 2, "name": "HD"}]
    assert RadarrService._resolve_profile_id(profiles, 2) == 2
    assert RadarrService._resolve_profile_id(profiles, "2") == 2


def test_radarr_resolve_profile_id_partial_name() -> None:
    profiles = [{"id": 1, "name": "SD"}, {"id": 2, "name": "Ultra-HD"}]
    assert RadarrService._resolve_profile_id(profiles, "ultra") == 2


def test_radarr_resolve_profile_id_missing_returns_none() -> None:
    profiles = [{"id": 1, "name": "SD"}]
    assert RadarrService._resolve_profile_id(profiles, "8k") is None


def test_radarr_resolve_profile_id_empty_falls_back_to_first() -> None:
    profiles = [{"id": 7, "name": "Default"}]
    assert RadarrService._resolve_profile_id(profiles, None) == 7
    assert RadarrService._resolve_profile_id(profiles, "") == 7


# ── Sonarr behaviour ─────────────────────────────────────────────────


async def test_sonarr_disabled_returns_error_message() -> None:
    svc = SonarrService()
    result = await svc.execute_tool("sonarr_list", {})
    assert "not configured" in result.lower()


async def test_sonarr_search_formats_results(sonarr: SonarrService) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/series/lookup",
        [{
            "title": "Breaking Bad", "year": 2008, "seasonCount": 5,
            "status": "ended", "tvdbId": 81189, "images": [],
            "overview": "A chemistry teacher diagnosed with cancer...",
        }],
    )
    result = await sonarr.execute_tool("sonarr_search", {"query": "breaking"})
    assert "Breaking Bad" in result
    assert "tvdb_id: 81189" in result


async def test_sonarr_list_formats_progress(sonarr: SonarrService) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/series",
        [{
            "id": 1, "title": "Fringe", "monitored": True,
            "statistics": {
                "episodeFileCount": 80, "totalEpisodeCount": 100,
                "percentOfEpisodes": 80.0,
            },
        }],
    )
    result = await sonarr.execute_tool("sonarr_list", {})
    assert "Fringe" in result
    assert "80/100" in result


async def test_sonarr_add_happy_path(sonarr: SonarrService) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/series/lookup",
        [{"title": "Severance", "tvdbId": 371980, "id": 0, "images": []}],
    )
    client.set("GET", "/rootfolder", [{"path": "/tv"}])
    client.set(
        "GET", "/qualityprofile",
        [{"id": 4, "name": "Any"}, {"id": 5, "name": "HD-1080p"}],
    )
    client.set("POST", "/series", {"title": "Severance"})

    result = await sonarr.execute_tool(
        "sonarr_add",
        {"tvdb_id": 371980, "quality_profile": "1080p"},
    )
    assert "Added" in result
    post_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/series"]
    payload = post_calls[0][2]
    assert payload is not None
    assert payload["qualityProfileId"] == 5
    assert payload["rootFolderPath"] == "/tv"
    assert payload["addOptions"]["searchForMissingEpisodes"] is True


async def test_sonarr_remove_deletes_files(sonarr: SonarrService) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    await sonarr.execute_tool("sonarr_remove", {"show": "12"})
    delete_calls = [c for c in client.calls if c[0] == "DELETE"]
    assert delete_calls[0][1] == "/series/12"
    assert delete_calls[0][2] == {"deleteFiles": "true"}


async def test_sonarr_grab_triggers_command(sonarr: SonarrService) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    await sonarr.execute_tool("sonarr_grab", {"show": 9})
    post_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/command"]
    assert post_calls[0][2] == {"name": "SeriesSearch", "seriesId": 9}


async def test_sonarr_details_resolves_by_name(sonarr: SonarrService) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set("GET", "/series", [{"id": 11, "title": "Lost"}])
    client.set(
        "GET", "/series/11",
        {
            "id": 11, "title": "Lost", "year": 2004, "status": "ended",
            "seasonCount": 6, "network": "ABC", "monitored": True,
            "statistics": {"episodeFileCount": 120, "totalEpisodeCount": 121},
            "images": [],
        },
    )
    result = await sonarr.execute_tool("sonarr_details", {"show": "lost"})
    assert "Lost" in result
    assert "120/121" in result


# ── Test connection action ──────────────────────────────────────────


def test_radarr_declares_test_connection_action() -> None:
    svc = RadarrService()
    actions = svc.config_actions()
    assert len(actions) == 1
    assert actions[0].key == "test_connection"
    assert actions[0].label == "Test connection"
    assert actions[0].required_role == "admin"


def test_sonarr_declares_test_connection_action() -> None:
    svc = SonarrService()
    actions = svc.config_actions()
    assert len(actions) == 1
    assert actions[0].key == "test_connection"
    assert actions[0].required_role == "admin"


async def test_radarr_test_connection_disabled_returns_error() -> None:
    svc = RadarrService()
    result = await svc.invoke_config_action("test_connection", {})
    assert result.status == "error"
    assert "disabled" in result.message.lower() or "not configured" in result.message.lower()


async def test_sonarr_test_connection_disabled_returns_error() -> None:
    svc = SonarrService()
    result = await svc.invoke_config_action("test_connection", {})
    assert result.status == "error"


async def test_radarr_test_connection_ok_returns_version(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/system/status",
        {"appName": "Radarr", "version": "5.4.1.8668"},
    )
    client.set(
        "GET", "/qualityprofile",
        [{"id": 1, "name": "HD"}, {"id": 2, "name": "Ultra-HD"}],
    )
    client.set("GET", "/rootfolder", [{"path": "/movies"}, {"path": "/4k"}])

    result = await radarr.invoke_config_action("test_connection", {})
    assert result.status == "ok"
    assert "Radarr" in result.message
    assert "5.4.1.8668" in result.message
    # Side effect: caches populated so dropdowns work on next render
    assert radarr._profile_choices == ("HD", "Ultra-HD")
    assert radarr._root_folder_choices == ("/movies", "/4k")


async def test_sonarr_test_connection_ok_returns_version(sonarr: SonarrService) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/system/status",
        {"appName": "Sonarr", "version": "4.0.9.2332"},
    )
    client.set("GET", "/qualityprofile", [{"id": 1, "name": "Any"}])
    client.set("GET", "/rootfolder", [{"path": "/tv"}])

    result = await sonarr.invoke_config_action("test_connection", {})
    assert result.status == "ok"
    assert "Sonarr" in result.message
    assert "4.0.9.2332" in result.message
    assert sonarr._profile_choices == ("Any",)
    assert sonarr._root_folder_choices == ("/tv",)


async def test_radarr_test_connection_401_reports_api_key_rejected(
    radarr: RadarrService,
) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set_error("GET", "/system/status", _http_status_error(401))

    result = await radarr.invoke_config_action("test_connection", {})
    assert result.status == "error"
    assert "API key" in result.message


async def test_sonarr_test_connection_403_reports_api_key_rejected(
    sonarr: SonarrService,
) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set_error("GET", "/system/status", _http_status_error(403))

    result = await sonarr.invoke_config_action("test_connection", {})
    assert result.status == "error"
    assert "API key" in result.message


async def test_radarr_test_connection_500_reports_http_code(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set_error("GET", "/system/status", _http_status_error(500))

    result = await radarr.invoke_config_action("test_connection", {})
    assert result.status == "error"
    assert "500" in result.message


async def test_radarr_test_connection_network_error_reports_failure(
    radarr: RadarrService,
) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set_error(
        "GET", "/system/status", httpx.ConnectError("unreachable"),
    )

    result = await radarr.invoke_config_action("test_connection", {})
    assert result.status == "error"
    assert "unreachable" in result.message.lower() or "failed" in result.message.lower()


async def test_radarr_unknown_action_returns_error(radarr: RadarrService) -> None:
    result = await radarr.invoke_config_action("nope", {})
    assert result.status == "error"
    assert "Unknown action" in result.message


# ── Dropdown choices from live service ──────────────────────────────


async def test_radarr_config_params_expose_cached_choices(radarr: RadarrService) -> None:
    # Before refresh, no choices
    params = {p.key: p for p in radarr.config_params()}
    assert params["default_quality_profile"].choices is None
    assert params["default_root_folder"].choices is None

    # Simulate refresh by directly seeding caches
    radarr._profile_choices = ("HD", "Ultra-HD")
    radarr._root_folder_choices = ("/movies", "/4k")

    params = {p.key: p for p in radarr.config_params()}
    assert params["default_quality_profile"].choices == ("HD", "Ultra-HD")
    assert params["default_root_folder"].choices == ("/movies", "/4k")


async def test_sonarr_config_params_expose_cached_choices(sonarr: SonarrService) -> None:
    params = {p.key: p for p in sonarr.config_params()}
    assert params["default_quality_profile"].choices is None

    sonarr._profile_choices = ("HD-1080p",)
    sonarr._root_folder_choices = ("/tv",)

    params = {p.key: p for p in sonarr.config_params()}
    assert params["default_quality_profile"].choices == ("HD-1080p",)
    assert params["default_root_folder"].choices == ("/tv",)


async def test_radarr_refresh_choices_survives_partial_failure(
    radarr: RadarrService,
) -> None:
    """If one of the two lookups fails, the other should still populate."""
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/qualityprofile", [{"id": 1, "name": "HD"}],
    )
    client.set_error(
        "GET", "/rootfolder", httpx.ConnectError("root folder boom"),
    )

    await radarr._refresh_choices()
    assert radarr._profile_choices == ("HD",)
    assert radarr._root_folder_choices == ()


async def test_radarr_refresh_choices_filters_empty_names(
    radarr: RadarrService,
) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/qualityprofile",
        [{"id": 1, "name": "HD"}, {"id": 2, "name": ""}, {"id": 3}],
    )
    client.set(
        "GET", "/rootfolder",
        [{"path": "/movies"}, {"path": ""}, {}],
    )

    await radarr._refresh_choices()
    assert radarr._profile_choices == ("HD",)
    assert radarr._root_folder_choices == ("/movies",)


async def test_radarr_stop_clears_choices(radarr: RadarrService) -> None:
    radarr._profile_choices = ("HD",)
    radarr._root_folder_choices = ("/movies",)
    await radarr.stop()
    assert radarr._profile_choices == ()
    assert radarr._root_folder_choices == ()
    assert radarr._enabled is False


# ── /radarr.find + /sonarr.find interactive picker ─────────────────


async def test_radarr_find_requires_query(radarr: RadarrService) -> None:
    result = await radarr.execute_tool("radarr_find", {})
    assert isinstance(result, str)
    assert "specify a movie" in result.lower()


async def test_radarr_find_empty_results_returns_string(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set("GET", "/movie/lookup", [])
    result = await radarr.execute_tool("radarr_find", {"query": "noise"})
    assert isinstance(result, str)
    assert "noise" in result.lower()


async def test_radarr_find_returns_tooloutput_with_blocks(
    radarr: RadarrService,
) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie/lookup",
        [
            {
                "title": "Inception", "year": 2010, "runtime": 148,
                "tmdbId": 27205, "id": 0,
                "overview": "A thief who steals corporate secrets via dream-sharing tech.",
                "images": [
                    {"coverType": "poster", "remoteUrl": "http://img/inception.jpg"},
                ],
            },
            {
                "title": "Dune", "year": 2021, "runtime": 155,
                "tmdbId": 438631, "id": 0, "overview": "",
                "images": [],
            },
        ],
    )
    result = await radarr.execute_tool("radarr_find", {"query": "inc"})
    assert isinstance(result, ToolOutput)
    assert len(result.ui_blocks) == 2
    assert "Add to Radarr" in result.text

    # First block — Inception
    block = result.ui_blocks[0]
    assert isinstance(block, UIBlock)
    assert block.title == "Add Inception (2010) to Radarr?"
    assert block.submit_label == "Add to Radarr"
    # Poster is a dedicated image element with constrained size
    image = next(e for e in block.elements if e.type == "image")
    assert image.url == "http://img/inception.jpg"
    assert image.max_width == 96
    assert image.label == "Inception"
    # Label element has plain-text meta (no markdown image syntax)
    label = next(e for e in block.elements if e.type == "label")
    assert "Inception" in label.label
    assert "![" not in label.label
    assert "http://img/inception.jpg" not in label.label
    assert "TMDB id: 27205" in label.label
    # Button element named tmdb_id with the TMDB id as its value
    buttons = next(e for e in block.elements if e.type == "buttons")
    assert buttons.name == "tmdb_id"
    assert len(buttons.options) == 1
    assert buttons.options[0].value == "27205"
    assert buttons.options[0].label == "Add to Radarr"


async def test_radarr_find_caps_at_five_candidates(radarr: RadarrService) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie/lookup",
        [
            {"title": f"Movie {i}", "year": 2000 + i, "tmdbId": i, "id": 0, "images": []}
            for i in range(10)
        ],
    )
    result = await radarr.execute_tool("radarr_find", {"query": "m"})
    assert isinstance(result, ToolOutput)
    assert len(result.ui_blocks) == 5


async def test_radarr_find_skips_add_button_when_already_in_library(
    radarr: RadarrService,
) -> None:
    client: FakeArrClient = radarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/movie/lookup",
        [{
            "title": "Owned Movie", "year": 2001, "tmdbId": 1, "id": 99,
            "images": [],
        }],
    )
    result = await radarr.execute_tool("radarr_find", {"query": "owned"})
    assert isinstance(result, ToolOutput)
    block = result.ui_blocks[0]
    # label still present...
    assert any(e.type == "label" for e in block.elements)
    # ...but no button element (can't add what you already have)
    assert not any(e.type == "buttons" for e in block.elements)
    # Explanation shows in the label
    label = next(e for e in block.elements if e.type == "label")
    assert "Already in your library" in label.label


async def test_sonarr_find_requires_query(sonarr: SonarrService) -> None:
    result = await sonarr.execute_tool("sonarr_find", {})
    assert isinstance(result, str)
    assert "specify a show" in result.lower()


async def test_sonarr_find_returns_tooloutput_with_blocks(
    sonarr: SonarrService,
) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/series/lookup",
        [{
            "title": "Severance", "year": 2022, "seasonCount": 2,
            "status": "continuing", "tvdbId": 371980, "id": 0,
            "overview": "Innies and outies at Lumon Industries.",
            "images": [
                {"coverType": "poster", "remoteUrl": "http://img/severance.jpg"},
            ],
        }],
    )
    result = await sonarr.execute_tool("sonarr_find", {"query": "sev"})
    assert isinstance(result, ToolOutput)
    assert len(result.ui_blocks) == 1

    block = result.ui_blocks[0]
    assert block.title == "Add Severance (2022) to Sonarr?"
    assert block.submit_label == "Add to Sonarr"
    image = next(e for e in block.elements if e.type == "image")
    assert image.url == "http://img/severance.jpg"
    assert image.max_width == 96
    buttons = next(e for e in block.elements if e.type == "buttons")
    assert buttons.name == "tvdb_id"
    assert buttons.options[0].value == "371980"
    assert buttons.options[0].label == "Add to Sonarr"


async def test_sonarr_find_skips_add_button_when_already_in_library(
    sonarr: SonarrService,
) -> None:
    client: FakeArrClient = sonarr._client  # type: ignore[assignment]
    client.set(
        "GET", "/series/lookup",
        [{
            "title": "Owned Show", "year": 2010, "tvdbId": 1, "id": 42,
            "seasonCount": 3, "images": [],
        }],
    )
    result = await sonarr.execute_tool("sonarr_find", {"query": "owned"})
    assert isinstance(result, ToolOutput)
    block = result.ui_blocks[0]
    assert not any(e.type == "buttons" for e in block.elements)
