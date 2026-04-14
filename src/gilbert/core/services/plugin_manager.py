"""Plugin manager service — install/uninstall plugins at runtime.

Exposes:

- The ``plugin_manager`` capability (a single PluginManagerService).
- ``ai_tools`` capability via three slash-grouped tools (``/plugin
  install``, ``/plugin uninstall``, ``/plugin list``).
- ``ws_handlers`` capability for the ``plugins.*`` WebSocket RPC
  namespace used by the ``/plugins`` settings page.

State persistence: an ``installed_plugins`` row per runtime-installed
plugin lives in entity storage so that uninstall-after-restart and
re-loading installed plugins both work seamlessly.

Note: plugins fetched into ``installed-plugins/`` are also discovered
by the boot-time scan in ``Gilbert._load_plugins``.  This service does
not duplicate that load — instead, on ``start()`` it reconciles the
DB rows with the plugins ``Gilbert`` already loaded so it can attribute
each one to its install source.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gilbert.interfaces.plugin import Plugin
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Query, StorageBackend, StorageProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
)
from gilbert.interfaces.ws import WsHandlerProvider
from gilbert.plugins.loader import (
    InstalledPluginInfo,
    PluginError,
    PluginLoader,
)

logger = logging.getLogger(__name__)

# Entity storage collection used for the runtime install registry.
_INSTALL_COLLECTION = "gilbert.plugin_installs"

# Default install directory (relative to working directory). This is one
# of the three plugin directories scanned at boot — see gilbert.yaml.
DEFAULT_INSTALL_DIR = Path("installed-plugins")


@dataclass
class _RuntimeRecord:
    """In-memory record of a plugin installed via this service."""

    name: str
    version: str
    description: str
    source_url: str
    install_path: Path
    installed_at: str
    registered_services: list[str] = field(default_factory=list)
    # Set when a plugin declares third-party Python deps in its own
    # ``pyproject.toml``. Such plugins cannot be hot-loaded because the
    # new deps aren't in the running venv yet — they need a restart so
    # ``gilbert.sh start`` re-runs ``uv sync`` first. The record is
    # still persisted so the UI can surface the pending state, and the
    # boot-time loader picks the plugin up normally on the next start.
    needs_restart: bool = False


class PluginManagerService(Service, ToolProvider, WsHandlerProvider):
    """Runtime plugin install/uninstall service.

    Capabilities: ``plugin_manager``, ``ai_tools``, ``ws_handlers``.
    Optional dependencies: ``entity_storage`` (registry persistence).
    """

    def __init__(self, install_dir: Path | str | None = None) -> None:
        self._install_dir: Path = (
            Path(install_dir) if install_dir is not None else DEFAULT_INSTALL_DIR
        ).resolve()
        self._loader: PluginLoader = PluginLoader()
        self._resolver: ServiceResolver | None = None
        self._storage: StorageBackend | None = None
        # Records loaded from the registry, keyed by plugin name.
        self._records: dict[str, _RuntimeRecord] = {}

    # --- Service lifecycle ---

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="plugin_manager",
            capabilities=frozenset({"plugin_manager", "ai_tools", "ws_handlers"}),
            optional=frozenset({"entity_storage"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        self._install_dir.mkdir(parents=True, exist_ok=True)

        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None and isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend

        await self._load_registry()
        logger.info(
            "PluginManagerService started — install_dir=%s, %d registry rows",
            self._install_dir,
            len(self._records),
        )

    async def reconcile_loaded_plugins(self, gilbert: Any) -> None:
        """Clear ``needs_restart`` flags for plugins that loaded successfully.

        Called after boot-time plugin loading has finished. If a plugin
        that was previously marked ``needs_restart=True`` now appears in
        the app's loaded plugin list, the deferred-load worked and we
        should persist the cleared flag plus the newly-registered
        services so the UI stops showing the restart warning.
        """
        loaded_names = {
            entry.plugin.metadata().name for entry in gilbert.list_loaded_plugins()
        }
        for name, record in list(self._records.items()):
            if not record.needs_restart:
                continue
            if name not in loaded_names:
                continue
            entry = gilbert.find_loaded_plugin(name)
            if entry is None:
                continue
            record.needs_restart = False
            record.registered_services = list(entry.registered_services)
            await self._persist_record(record)
            logger.info(
                "Plugin %s finished deferred load (registered: %s)",
                name, record.registered_services,
            )

    async def stop(self) -> None:
        # Plugin lifecycle is managed by the Gilbert app, not by this
        # service. Nothing to do on stop.
        pass

    # --- Registry persistence ---

    async def _load_registry(self) -> None:
        """Read the install registry into ``self._records``.

        Drops rows whose install directory has gone missing (e.g. the
        user manually deleted the directory). Does NOT trigger plugin
        loading — boot-time scanning already handled that.
        """
        if self._storage is None:
            return
        try:
            rows = await self._storage.query(Query(collection=_INSTALL_COLLECTION))
        except Exception:
            logger.exception("Failed to load plugin install registry")
            return

        for row in rows:
            name = str(row.get("_id") or row.get("name") or "")
            if not name:
                continue
            install_path_str = str(row.get("install_path") or "")
            install_path = Path(install_path_str) if install_path_str else self._install_dir / name
            if not install_path.exists():
                logger.warning(
                    "Registry row references missing install dir, dropping: %s -> %s",
                    name, install_path,
                )
                try:
                    await self._storage.delete(_INSTALL_COLLECTION, name)
                except Exception:
                    logger.exception("Failed to drop stale registry row: %s", name)
                continue
            self._records[name] = _RuntimeRecord(
                name=name,
                version=str(row.get("version") or ""),
                description=str(row.get("description") or ""),
                source_url=str(row.get("source_url") or ""),
                install_path=install_path,
                installed_at=str(row.get("installed_at") or ""),
                registered_services=list(row.get("registered_services") or []),
                needs_restart=bool(row.get("needs_restart", False)),
            )

    async def _persist_record(self, record: _RuntimeRecord) -> None:
        if self._storage is None:
            return
        await self._storage.put(
            _INSTALL_COLLECTION,
            record.name,
            {
                "_id": record.name,
                "name": record.name,
                "version": record.version,
                "description": record.description,
                "source_url": record.source_url,
                "install_path": str(record.install_path),
                "installed_at": record.installed_at,
                "registered_services": list(record.registered_services),
                "needs_restart": record.needs_restart,
            },
        )

    async def _delete_record(self, name: str) -> None:
        if self._storage is None:
            return
        try:
            await self._storage.delete(_INSTALL_COLLECTION, name)
        except Exception:
            logger.exception("Failed to delete registry row: %s", name)

    # --- Public install / uninstall API ---

    async def install(
        self,
        gilbert: Any,
        source_url: str,
        *,
        force: bool = False,
    ) -> _RuntimeRecord:
        """Install a plugin from a URL and hot-load its services.

        ``gilbert`` is the running ``Gilbert`` app instance — needed to
        build a ``PluginContext`` matching the boot-time loader's
        contract and to register the loaded plugin in the app's
        ``LoadedPlugin`` list so future uninstall calls can find it.

        Returns the new ``_RuntimeRecord`` on success.
        """
        info: InstalledPluginInfo = await self._loader.install_from_url(
            source_url, self._install_dir, force=force,
        )
        installed_at = datetime.now(UTC).isoformat()

        # If the plugin declares third-party Python deps in its own
        # ``pyproject.toml``, we can't hot-load it — the new deps aren't
        # in the running venv yet. Record the install so the UI can show
        # "restart required" and ``gilbert.sh start`` picks it up on the
        # next boot (via ``uv sync`` re-resolving the workspace).
        if _plugin_declares_python_deps(info.install_path):
            logger.info(
                "Plugin %s declares Python dependencies — deferring load "
                "until next restart so ``uv sync`` can install them.",
                info.name,
            )
            record = _RuntimeRecord(
                name=info.name,
                version=info.version,
                description=info.description,
                source_url=source_url,
                install_path=info.install_path,
                installed_at=installed_at,
                registered_services=[],
                needs_restart=True,
            )
            await self._persist_record(record)
            self._records[info.name] = record
            return record

        registered: list[str] = []
        plugin: Plugin | None = None
        sm = gilbert.service_manager
        # Snapshot the registered services *before* anything plugin-side
        # runs, so the rollback path can clean up services even if
        # setup() raises partway through.
        before = set(sm.list_services().keys())

        try:
            # Load the plugin module from its final on-disk location.
            plugin = self._loader.load_from_manifest(info.manifest)

            context = gilbert.make_plugin_context(info.name)
            await plugin.setup(context)

            after = set(sm.list_services().keys())
            registered = sorted(after - before)

            # Start each newly-registered service. Plugin setup() typically
            # only registers — we drive the start lifecycle here.
            for svc_name in registered:
                try:
                    await sm.start_service(svc_name)
                except Exception:
                    logger.exception(
                        "Failed to start service %s from plugin %s",
                        svc_name, info.name,
                    )
                    raise

            # Record in the app's loaded-plugin list so it gets torn
            # down on app shutdown alongside boot-loaded plugins.
            from gilbert.core.app import LoadedPlugin

            gilbert.add_loaded_plugin(LoadedPlugin(
                plugin=plugin,
                install_path=info.install_path,
                registered_services=registered,
            ))

            record = _RuntimeRecord(
                name=info.name,
                version=info.version,
                description=info.description,
                source_url=source_url,
                install_path=info.install_path,
                installed_at=installed_at,
                registered_services=registered,
            )
            await self._persist_record(record)
            self._records[info.name] = record
            logger.info(
                "Plugin installed and loaded: %s v%s (services: %s)",
                info.name, info.version, registered,
            )
            return record

        except Exception:
            # Roll back: tear down anything we registered, drop the
            # directory, leave the registry clean.
            logger.exception("Plugin install failed for %s — rolling back", info.name)
            # Recompute the diff in case setup() registered services
            # before raising (so ``registered`` was never populated).
            after_failed = set(sm.list_services().keys())
            for svc_name in sorted(after_failed - before):
                try:
                    await sm.stop_and_unregister(svc_name)
                except Exception:
                    logger.exception("Rollback: failed to unregister %s", svc_name)
            if plugin is not None:
                try:
                    await plugin.teardown()
                except Exception:
                    logger.exception("Rollback: plugin teardown raised")
            try:
                await self._loader.uninstall(info.name, self._install_dir)
            except Exception:
                logger.exception("Rollback: failed to remove install dir for %s", info.name)
            self._purge_plugin_modules(info.name)
            raise

    async def uninstall(self, gilbert: Any, name: str) -> None:
        """Stop & unregister a runtime-installed plugin and remove its files.

        Raises ``LookupError`` if the plugin is not known to this
        service (i.e. it lives in std-plugins or local-plugins, not
        installed-plugins).
        """
        record = self._records.get(name)
        if record is None:
            raise LookupError(f"Plugin not installed by manager: {name}")

        loaded = gilbert.find_loaded_plugin(name)
        sm = gilbert.service_manager

        if loaded is not None:
            try:
                await loaded.plugin.teardown()
            except Exception:
                logger.exception("Plugin teardown raised: %s", name)
            for svc_name in loaded.registered_services:
                try:
                    await sm.stop_and_unregister(svc_name)
                except Exception:
                    logger.exception(
                        "Failed to stop/unregister service %s for plugin %s",
                        svc_name, name,
                    )
            gilbert.remove_loaded_plugin(name)
        else:
            # Fall back to whatever the registry said about services.
            for svc_name in record.registered_services:
                try:
                    await sm.stop_and_unregister(svc_name)
                except LookupError:
                    pass
                except Exception:
                    logger.exception(
                        "Failed to stop/unregister service %s for plugin %s",
                        svc_name, name,
                    )

        await self._delete_record(name)
        self._records.pop(name, None)

        try:
            await self._loader.uninstall(name, self._install_dir)
        except Exception:
            logger.exception("Failed to remove install dir for %s", name)

        self._purge_plugin_modules(name)
        logger.info("Plugin uninstalled: %s", name)

    def list_installed(self, gilbert: Any) -> list[dict[str, Any]]:
        """Return one row per known plugin (boot-loaded + runtime-installed).

        ``source`` buckets by which configured plugin directory the
        install path lives under: ``"std"``, ``"local"``, ``"installed"``,
        or ``"unknown"`` if it doesn't match any.
        """
        bucket_dirs = self._resolve_bucket_dirs(gilbert)
        results: list[dict[str, Any]] = []
        loaded_names: set[str] = set()

        for entry in gilbert.list_loaded_plugins():
            meta = entry.plugin.metadata()
            loaded_names.add(meta.name)
            record = self._records.get(meta.name)
            results.append({
                "name": meta.name,
                "version": meta.version,
                "description": meta.description,
                "install_path": str(entry.install_path),
                "source": _bucket_for(entry.install_path, bucket_dirs),
                "source_url": record.source_url if record else None,
                "installed_at": record.installed_at if record else None,
                "registered_services": list(entry.registered_services),
                "running": True,
                "uninstallable": meta.name in self._records,
                "needs_restart": bool(record and record.needs_restart),
            })

        # Registry rows whose plugins didn't actually load (e.g. a previous
        # boot-time load failed, or a deferred install waiting for restart).
        # Surface them so the user can clean up or restart.
        for name, record in self._records.items():
            if name in loaded_names:
                continue
            results.append({
                "name": record.name,
                "version": record.version,
                "description": record.description,
                "install_path": str(record.install_path),
                "source": _bucket_for(record.install_path, bucket_dirs),
                "source_url": record.source_url,
                "installed_at": record.installed_at,
                "registered_services": list(record.registered_services),
                "running": False,
                "uninstallable": True,
                "needs_restart": record.needs_restart,
            })

        results.sort(key=lambda r: r["name"])
        return results

    def _resolve_bucket_dirs(self, gilbert: Any) -> dict[str, Path]:
        """Map source bucket name → resolved absolute directory path."""
        configured: list[str] = list(gilbert.config.plugins.directories)
        out: dict[str, Path] = {}
        for d in configured:
            resolved = Path(d).expanduser().resolve()
            base = Path(d).name
            if base == "std-plugins":
                out["std"] = resolved
            elif base == "local-plugins":
                out["local"] = resolved
            elif base == "installed-plugins":
                out["installed"] = resolved
            else:
                out[base] = resolved
        # The install_dir we manage may not be one of the configured
        # bucket dirs (e.g. in tests). Track it as ``installed`` so the
        # source classification still works.
        out.setdefault("installed", self._install_dir)
        return out

    def _purge_plugin_modules(self, name: str) -> None:
        """Drop any sys.modules entries for a plugin so a re-install
        re-imports the code from disk instead of getting a stale cached
        module from a prior load."""
        sanitized = name.replace("-", "_")
        prefix = f"gilbert_plugin_{sanitized}"
        for mod_name in list(sys.modules):
            if mod_name == prefix or mod_name.startswith(prefix + "."):
                sys.modules.pop(mod_name, None)

    # --- ToolProvider interface ---

    @property
    def tool_provider_name(self) -> str:
        return "plugin_manager"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="plugin_install",
                slash_group="plugin",
                slash_command="install",
                slash_help=(
                    "Install a plugin from a GitHub URL or archive: "
                    "/plugin install <url>"
                ),
                description=(
                    "Download and install a plugin at runtime from a "
                    "GitHub URL (whole-repo or /tree/<ref>/<subpath>) "
                    "or an archive URL (.zip, .tar.gz, .tgz, .tar.bz2). "
                    "Validates the manifest, hot-loads the plugin, and "
                    "registers its services without restarting Gilbert."
                ),
                parameters=[
                    ToolParameter(
                        name="url",
                        type=ToolParameterType.STRING,
                        description="GitHub URL or archive URL to install from.",
                    ),
                    ToolParameter(
                        name="force",
                        type=ToolParameterType.BOOLEAN,
                        description=(
                            "Reinstall over an existing installation of "
                            "the same name."
                        ),
                        required=False,
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="plugin_uninstall",
                slash_group="plugin",
                slash_command="uninstall",
                slash_help="Uninstall a runtime-installed plugin: /plugin uninstall <name>",
                description=(
                    "Stop and remove a previously installed plugin, "
                    "unregister all of its services, and delete its "
                    "directory from installed-plugins/. Only plugins "
                    "installed via this service can be uninstalled — "
                    "plugins from std-plugins/ or local-plugins/ are "
                    "managed outside the runtime."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="The plugin name (as in plugin.yaml).",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="plugin_list",
                slash_group="plugin",
                slash_command="list",
                slash_help="List installed plugins: /plugin list",
                description=(
                    "List all known plugins (boot-loaded from std-plugins, "
                    "local-plugins, or installed-plugins, plus anything "
                    "installed at runtime). Shows version, source, and "
                    "running state for each."
                ),
                required_role="admin",
            ),
            ToolDefinition(
                name="plugin_restart_host",
                slash_group="plugin",
                slash_command="restart",
                slash_help="Restart Gilbert to finish a deferred install: /plugin restart",
                description=(
                    "Gracefully stop Gilbert so the ``gilbert.sh`` "
                    "supervisor loop re-runs ``uv sync`` and relaunches "
                    "it. Use this after ``/plugin install`` reports "
                    "``needs_restart`` — the newly installed plugin's "
                    "Python dependencies will be installed during the "
                    "restart cycle and the plugin will load on the next "
                    "boot. Only works when Gilbert is running under "
                    "``./gilbert.sh start``; running ``python -m "
                    "gilbert`` directly will just exit."
                ),
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        gilbert = self._require_gilbert()
        match name:
            case "plugin_install":
                url = str(arguments.get("url") or "").strip()
                if not url:
                    raise ValueError("plugin_install requires 'url'")
                force = bool(arguments.get("force", False))
                try:
                    record = await self.install(gilbert, url, force=force)
                except PluginError as exc:
                    return json.dumps({"status": "error", "error": str(exc)})
                return json.dumps({
                    "status": (
                        "installed_restart_required"
                        if record.needs_restart
                        else "installed"
                    ),
                    "name": record.name,
                    "version": record.version,
                    "source_url": record.source_url,
                    "registered_services": record.registered_services,
                    "needs_restart": record.needs_restart,
                    "message": (
                        f"Plugin {record.name} v{record.version} installed. "
                        "It declares third-party Python dependencies, so a "
                        "restart is required to finish loading it — run "
                        "``/plugin restart`` to trigger the supervised "
                        "restart (``gilbert.sh`` will re-run ``uv sync`` "
                        "and relaunch automatically)."
                        if record.needs_restart
                        else f"Plugin {record.name} v{record.version} installed and loaded."
                    ),
                })
            case "plugin_uninstall":
                target = str(arguments.get("name") or "").strip()
                if not target:
                    raise ValueError("plugin_uninstall requires 'name'")
                try:
                    await self.uninstall(gilbert, target)
                except LookupError as exc:
                    return json.dumps({"status": "error", "error": str(exc)})
                return json.dumps({"status": "uninstalled", "name": target})
            case "plugin_list":
                rows = self.list_installed(gilbert)
                return json.dumps({"plugins": rows})
            case "plugin_restart_host":
                pending = [
                    r.name for r in self._records.values() if r.needs_restart
                ]
                gilbert.request_restart()
                return json.dumps({
                    "status": "restart_requested",
                    "pending_plugins": pending,
                    "message": (
                        "Gilbert is shutting down. The supervisor loop "
                        "in gilbert.sh will re-run ``uv sync`` and "
                        "relaunch automatically. "
                        + (
                            f"Deferred plugins waiting to load: {', '.join(pending)}."
                            if pending
                            else "No deferred plugins are waiting."
                        )
                    ),
                })
            case _:
                raise KeyError(f"Unknown tool: {name}")

    def _require_gilbert(self) -> Any:
        """Find the running ``Gilbert`` app via the resolver.

        Tools execute outside a WebSocket connection, so we can't fall
        back on ``conn.manager.gilbert`` like WS handlers can. We rely
        on a small attribute set during ``Gilbert.start()`` — see
        ``Gilbert._wire_plugin_manager()`` — that stores the app on the
        service.
        """
        gilbert = getattr(self, "_gilbert", None)
        if gilbert is None:
            raise RuntimeError(
                "PluginManagerService is not bound to a Gilbert app",
            )
        return gilbert

    def bind_gilbert(self, gilbert: Any) -> None:
        """Called by ``Gilbert.start()`` so tools can reach the app."""
        self._gilbert = gilbert

    # --- WsHandlerProvider interface ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "plugins.list": self._ws_plugins_list,
            "plugins.install": self._ws_plugins_install,
            "plugins.uninstall": self._ws_plugins_uninstall,
            "plugins.restart_host": self._ws_plugins_restart_host,
        }

    async def _ws_plugins_list(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {"type": "plugins.list.result", "ref": frame.get("id"), "plugins": []}
        return {
            "type": "plugins.list.result",
            "ref": frame.get("id"),
            "plugins": self.list_installed(gilbert),
        }

    async def _ws_plugins_install(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        gilbert = conn.manager.gilbert
        if gilbert is None:
            return _ws_error(frame, "Gilbert app not available", code=503)
        url = str(frame.get("url") or "").strip()
        if not url:
            return _ws_error(frame, "Missing 'url'")
        force = bool(frame.get("force", False))
        try:
            record = await self.install(gilbert, url, force=force)
        except PluginError as exc:
            return _ws_error(frame, str(exc), code=400)
        except Exception as exc:
            logger.exception("plugins.install failed")
            return _ws_error(frame, f"Install failed: {exc}", code=500)
        return {
            "type": "plugins.install.result",
            "ref": frame.get("id"),
            "plugin": {
                "name": record.name,
                "version": record.version,
                "description": record.description,
                "source_url": record.source_url,
                "install_path": str(record.install_path),
                "installed_at": record.installed_at,
                "registered_services": record.registered_services,
                "source": "installed",
                "running": not record.needs_restart,
                "uninstallable": True,
                "needs_restart": record.needs_restart,
            },
        }

    async def _ws_plugins_restart_host(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        """Ask the host process to exit with the restart exit code.

        The ``gilbert.sh`` supervisor loop catches the exit code, re-
        runs ``uv sync`` (installing any newly pulled plugin deps into
        the venv), and relaunches Gilbert. UIs call this after a
        ``plugins.install`` that returned ``needs_restart: true``.
        """
        gilbert = conn.manager.gilbert
        if gilbert is None:
            return _ws_error(frame, "Gilbert app not available", code=503)
        pending = [r.name for r in self._records.values() if r.needs_restart]
        gilbert.request_restart()
        return {
            "type": "plugins.restart_host.result",
            "ref": frame.get("id"),
            "status": "restart_requested",
            "pending_plugins": pending,
        }

    async def _ws_plugins_uninstall(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        gilbert = conn.manager.gilbert
        if gilbert is None:
            return _ws_error(frame, "Gilbert app not available", code=503)
        name = str(frame.get("name") or "").strip()
        if not name:
            return _ws_error(frame, "Missing 'name'")
        try:
            await self.uninstall(gilbert, name)
        except LookupError as exc:
            return _ws_error(frame, str(exc), code=404)
        except Exception as exc:
            logger.exception("plugins.uninstall failed")
            return _ws_error(frame, f"Uninstall failed: {exc}", code=500)
        return {
            "type": "plugins.uninstall.result",
            "ref": frame.get("id"),
            "name": name,
            "status": "uninstalled",
        }


# --- Module helpers ---


def _ws_error(frame: dict[str, Any], error: str, *, code: int = 400) -> dict[str, Any]:
    return {
        "type": "gilbert.error",
        "ref": frame.get("id"),
        "error": error,
        "code": code,
    }


def _plugin_declares_python_deps(plugin_dir: Path) -> bool:
    """Return True if the plugin ships a ``pyproject.toml`` with a
    non-empty ``[project].dependencies`` list.

    Used to decide whether a runtime-installed plugin can be hot-loaded
    (no third-party deps → safe to load into the running venv) or has
    to wait for the next ``gilbert.sh start`` to re-run ``uv sync``
    against the workspace.
    """
    pyproject = plugin_dir / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        import tomllib

        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        logger.debug(
            "Failed to parse %s — assuming no deps", pyproject, exc_info=True,
        )
        return False
    project = data.get("project", {})
    if not isinstance(project, dict):
        return False
    deps = project.get("dependencies", [])
    return bool(deps)


def _bucket_for(path: Path, bucket_dirs: dict[str, Path]) -> str:
    """Determine which configured bucket a plugin install path lives under."""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    for bucket, base in bucket_dirs.items():
        try:
            resolved.relative_to(base)
            return bucket
        except ValueError:
            continue
    return "unknown"
