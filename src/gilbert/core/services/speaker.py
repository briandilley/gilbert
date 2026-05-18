"""Speaker service — wraps a SpeakerBackend as a discoverable service with announce support."""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from gilbert.core.output import cleanup_old_files, get_output_dir
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import (
    LoopMode,
    NowPlaying,
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerInfo,
    split_speaker_id,
    to_browser_url,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Entity collection for speaker aliases
_ALIAS_COLLECTION = "speaker_aliases"

# Per-user preference key controlling whether speaker output also fans
# out to the user's connected browser tab. Stored on the user's
# ``metadata`` dict via ``UserPrefReader``. Namespaced so future per-
# user speaker prefs sit alongside without collision.
_BROWSER_ECHO_PREF_KEY = "speaker.browser_echo"


class SpeakerService(Service):
    """Exposes a SpeakerBackend as a service with speaker control and announce capabilities."""

    def __init__(self) -> None:
        self._backends: dict[str, SpeakerBackend] = {}
        self._backend_name: str = "sonos"
        self._primary_backend: str = ""
        self._enabled: bool = False
        self._config: dict[str, object] = {}
        self._output_ttl_seconds: int = 3600
        self._default_announce_volume: int | None = None
        self._default_announce_speakers: list[str] = []
        self._web_host: str = "0.0.0.0"
        self._web_port: int = 8000
        # Track last-used speaker set for "use last" default
        self._last_speaker_ids: list[str] = []
        # Per-speaker announcement locks. Each speaker has its own lock
        # so announcements targeting *different* speakers fan out, while
        # announcements targeting the *same* speaker still serialize
        # (otherwise snapshot/restore on that speaker would race and
        # two clips would talk over each other). Locks are created lazily
        # and acquired in sorted-ID order so two overlapping-set callers
        # can't deadlock. ``_speaker_locks_guard`` serializes the
        # get-or-create step so we don't race on dict insertion.
        self._speaker_locks: dict[str, asyncio.Lock] = {}
        self._speaker_locks_guard = asyncio.Lock()
        self._speaker_cache: list[SpeakerInfo] = []
        # Backend startup failures keyed by backend name. Populated by
        # ``_reinit_backends`` when a backend raises during initialization.
        self._startup_failures: dict[str, str] = {}
        # Wired in start() for the per-user browser-echo fan-out. Both
        # are optional — if either is missing (no user service, no
        # event bus) the fan-out silently no-ops. The primary backend
        # still gets the play_uri call.
        self._users_svc: Any = None
        self._event_bus_provider: Any = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="speaker",
            capabilities=frozenset({"speaker_control", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset(
                {"configuration", "text_to_speech", "users", "event_bus"}
            ),
            toggleable=True,
            toggle_description="Speaker playback and control",
        )

    @property
    def backends(self) -> Mapping[str, SpeakerBackend]:
        """Mapping of currently-loaded backends, keyed by ``backend_name``."""
        return self._backends

    def get_backend(self, name: str) -> SpeakerBackend | None:
        """Return a loaded backend by name, or ``None`` if not loaded."""
        return self.backends.get(name)

    async def resolve_names(self, names: list[str]) -> dict[str, str]:
        """Map speaker display names to namespaced speaker ids.

        Returns ``{name: "<backend>:<native>"}`` for each name that
        matches a known speaker. Names that don't match any speaker are
        omitted (callers decide whether that's an error).

        Delegates to ``list_speakers()`` which already returns namespaced
        ids, so no inline prefix stamping is needed here.
        """
        out: dict[str, str] = {}
        speakers = await self.list_speakers()  # already namespaced
        by_name = {s.name: s for s in speakers}
        for name in names:
            s = by_name.get(name)
            if s is not None:
                out[name] = s.speaker_id
        return out

    @property
    def cached_speakers(self) -> list[SpeakerInfo]:
        """Last-known speaker list (populated after start)."""
        return list(self._speaker_cache)

    async def list_speakers(self) -> list[SpeakerInfo]:
        """Return speakers across all loaded backends.

        Each ``SpeakerInfo.speaker_id`` is namespaced as ``<backend>:<native>``
        and ``backend_name`` is stamped. If any single backend's ``list_speakers``
        raises, the failure is logged and that backend's slice is omitted; other
        backends still contribute.
        """
        if not self._backends:
            return []
        items = list(self._backends.items())
        results = await asyncio.gather(
            *(b.list_speakers() for _, b in items),
            return_exceptions=True,
        )
        merged: list[SpeakerInfo] = []
        for (name, _), result in zip(items, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("speaker backend '%s' list_speakers failed: %s", name, result)
                continue
            for s in result:
                merged.append(replace(s, speaker_id=f"{name}:{s.speaker_id}", backend_name=name))
        return merged

    async def list_speaker_groups(self) -> list["SpeakerGroup"]:
        """Return groups across all loaded backends that support grouping.

        ``coordinator_id`` and every entry in ``member_ids`` are
        namespaced as ``<backend>:<native>`` to match what
        ``list_speakers()`` returns. Backends that don't support grouping
        are skipped. If a grouping-capable backend's ``list_groups`` raises,
        the failure is logged and that backend's slice is omitted.
        """
        from gilbert.interfaces.speaker import SpeakerGroup  # local import to avoid circular at module level

        if not self._backends:
            return []
        grouping = [(name, b) for name, b in self._backends.items() if b.supports_grouping]
        if not grouping:
            return []
        results = await asyncio.gather(
            *(b.list_groups() for _, b in grouping),
            return_exceptions=True,
        )
        merged: list[SpeakerGroup] = []
        for (name, _), result in zip(grouping, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("speaker backend '%s' list_groups failed: %s", name, result)
                continue
            for g in result:
                merged.append(
                    replace(
                        g,
                        coordinator_id=f"{name}:{g.coordinator_id}",
                        member_ids=[f"{name}:{m}" for m in g.member_ids],
                        backend_name=name,
                    )
                )
        return merged

    async def start(self, resolver: ServiceResolver) -> None:
        # Store resolver references for runtime use
        self._storage_svc = resolver.require_capability("entity_storage")
        self._tts_svc = resolver.get_capability("text_to_speech")

        # Load config
        section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                global_ttl = config_svc.get("output_ttl_seconds")
                if global_ttl is not None:
                    self._output_ttl_seconds = int(global_ttl)
                # Read web config for building audio URLs
                web_section = config_svc.get_section("web")
                self._web_host = web_section.get("host", "0.0.0.0")
                self._web_port = int(web_section.get("port", 8765))

        if not section.get("enabled", False):
            logger.info("Speaker service disabled")
            return

        self._enabled = True
        self._apply_config(section)

        # Side-effect imports so the bundled vendor-free backends
        # register. Third-party backends (Sonos, …) register via plugins.
        import gilbert.integrations.browser_speaker  # noqa: F401
        import gilbert.integrations.local_speaker  # noqa: F401

        # Stash the event-bus provider before _reinit_backends so any
        # EventBusAwareSpeakerBackend initialized there can be wired.
        from gilbert.interfaces.events import EventBusProvider

        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._event_bus_provider = bus_svc

        # User-prefs lookup for the browser-echo fan-out. Optional —
        # without it the fan-out helper bails early and only the
        # primary backend hears the play.
        from gilbert.interfaces.users import UserPrefReader

        users_svc = resolver.get_capability("users")
        if isinstance(users_svc, UserPrefReader):
            self._users_svc = users_svc

        # Initialize all configured backends.
        await self._reinit_backends(section.get("backends", {}))
        self._resolve_primary_backend(primary=section.get("primary_backend", ""))

        # Ensure alias index
        from gilbert.interfaces.storage import IndexDefinition

        storage = self._get_storage_backend()
        await storage.ensure_index(
            IndexDefinition(
                collection=_ALIAS_COLLECTION,
                fields=["alias"],
                unique=True,
            )
        )

        logger.info("Speaker service started")

    def _require_single_backend(self) -> SpeakerBackend:
        """Return a single loaded backend, or raise if none are loaded.

        When exactly one backend is loaded it is returned directly.
        When more than one is loaded the first by sorted name is returned
        and a warning is logged — callers that haven't been updated yet to
        use per-backend routing fall back gracefully. This is a transitional
        helper; Task 11 audits and removes most callers.
        """
        if not self._backends:
            raise RuntimeError("Speaker service is not enabled")
        if len(self._backends) > 1:
            first_name = sorted(self._backends)[0]
            logger.warning(
                "_require_single_backend called with %d backends loaded; "
                "picking %r. Caller should be updated to route per-backend.",
                len(self._backends),
                first_name,
            )
            return self._backends[first_name]
        return next(iter(self._backends.values()))

    def _get_storage_backend(self) -> Any:
        """Get the storage backend from the storage service."""
        from gilbert.interfaces.storage import StorageProvider

        if isinstance(self._storage_svc, StorageProvider):
            return self._storage_svc.backend
        raise TypeError("Expected StorageProvider for entity_storage")

    def _apply_config(self, section: dict[str, Any]) -> None:
        """Apply tunable config values."""
        self._config = section.get("settings", self._config)
        vol = section.get("default_announce_volume")
        if vol is not None:
            self._default_announce_volume = int(vol)
        spk = section.get("default_announce_speakers")
        if isinstance(spk, list):
            self._default_announce_speakers = spk

    def _resolve_primary_backend(self, *, primary: str) -> None:
        """Set ``self._primary_backend`` from the configured value.

        Falls back to the alphabetically-first loaded backend when the
        configured value is empty or points at a backend that isn't loaded.
        Logs a one-time WARN on fallback.
        """
        if primary and primary in self._backends:
            self._primary_backend = primary
            return
        candidates = sorted(self._backends)
        if not candidates:
            self._primary_backend = ""
            return
        chosen = candidates[0]
        if primary:
            logger.warning(
                "speaker.primary_backend=%r is not loaded; falling back to %r",
                primary,
                chosen,
            )
        else:
            logger.warning(
                "speaker.primary_backend not set; defaulting to %r",
                chosen,
            )
        self._primary_backend = chosen

    async def _refresh_cached_speakers(self) -> None:
        """Refresh the cached speaker list from all loaded backends.

        Swallows any exceptions so a backend that fails discovery on
        startup doesn't prevent other backends from being cached.
        """
        try:
            self._speaker_cache = await self.list_speakers()
        except Exception:
            logger.debug("Could not refresh speaker cache", exc_info=True)

    async def _reinit_backends(self, backends_config: dict[str, Any]) -> None:
        """Reinitialize backends from config, closing any that changed.

        Backends with ``enabled=False`` in their config section are skipped
        entirely. A backend whose name doesn't appear in ``backends_config``
        at all is treated as not configured and dropped.
        """
        from gilbert.interfaces.speaker import EventBusAwareSpeakerBackend

        if not isinstance(backends_config, dict):
            return
        for name, cls in SpeakerBackend.registered_backends().items():
            if name not in backends_config:
                old = self._backends.pop(name, None)
                if old is not None:
                    await old.close()
                    logger.info("speaker backend '%s' removed (no config section)", name)
                self._startup_failures.pop(name, None)
                continue
            cfg = backends_config.get(name, {})
            if not isinstance(cfg, dict):
                cfg = {}
            enabled = cfg.get("enabled", True) is True
            old = self._backends.get(name)
            if not enabled:
                if old is not None:
                    await old.close()
                    self._backends.pop(name, None)
                    logger.info("speaker backend '%s' disabled, closed", name)
                self._startup_failures.pop(name, None)
                continue
            try:
                inst = cls()
                # Wire event bus if backend wants it (e.g. BrowserSpeakerBackend
                # needs it to publish speaker.browser.* frames).
                if isinstance(inst, EventBusAwareSpeakerBackend) and self._event_bus_provider is not None:
                    inst.set_event_bus_provider(self._event_bus_provider)
                await inst.initialize(cfg)
                if old is not None:
                    await old.close()
                self._backends[name] = inst
                self._startup_failures.pop(name, None)
                logger.info("speaker backend '%s' (re)initialized", name)
            except Exception as exc:
                self._startup_failures[name] = str(exc)
                if old is None:
                    logger.warning("speaker backend '%s' failed to start: %s", name, exc)
        await self._refresh_cached_speakers()

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "speaker"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        # Side-effect imports so bundled backends register before we
        # iterate ``SpeakerBackend.registered_backends()``.
        import gilbert.integrations.browser_speaker  # noqa: F401
        import gilbert.integrations.local_speaker  # noqa: F401
        from gilbert.interfaces.speaker import SpeakerBackend

        params: list[ConfigParam] = [
            ConfigParam(
                key="default_announce_volume",
                type=ToolParameterType.INTEGER,
                description="Default volume level for announcements (0-100). Unset means use current volume.",
            ),
            ConfigParam(
                key="default_announce_speakers",
                type=ToolParameterType.ARRAY,
                description="Default speakers for announcements (empty = all).",
                default=[],
                choices_from="speakers",
            ),
            ConfigParam(
                key="primary_backend",
                type=ToolParameterType.STRING,
                description=(
                    "Receives bare announce()/play() calls without explicit "
                    "speaker targets. Defaults to the first alphabetically-ordered "
                    "enabled backend when unset."
                ),
                default="",
                choices_from="speakers.enabled_backends",
            ),
        ]
        # Per-backend sections (mirrors AIService.config_params pattern)
        for name, cls in sorted(SpeakerBackend.registered_backends().items()):
            params.append(
                ConfigParam(
                    key=f"backends.{name}.enabled",
                    type=ToolParameterType.BOOLEAN,
                    description=f"Enable the '{name}' speaker backend.",
                    default=False,
                    restart_required=True,
                )
            )
            for bp in cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"backends.{name}.{bp.key}",
                        type=bp.type,
                        description=f"[{name}] {bp.description}",
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        backend_param=True,
                        ai_prompt=bp.ai_prompt,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)
        await self._reinit_backends(config.get("backends", {}))
        self._resolve_primary_backend(primary=config.get("primary_backend", ""))

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=SpeakerBackend.registered_backends(),
            current_backend=self._backends.get(self._backend_name),
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backends.get(self._backend_name), key, payload)

    async def stop(self) -> None:
        for backend in list(self._backends.values()):
            await backend.close()

    # --- Alias management ---

    async def set_alias(self, speaker_id: str, alias: str) -> None:
        """Assign an alias name to a speaker. Raises ValueError on collision."""
        backend = self._require_single_backend()
        # Check the alias doesn't collide with an existing speaker name
        speakers = await backend.list_speakers()
        for s in speakers:
            if s.name.lower() == alias.lower():
                raise ValueError(f"Alias '{alias}' collides with existing speaker name '{s.name}'")

        # Check alias doesn't collide with another alias
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        existing = await storage.query(
            Query(
                collection=_ALIAS_COLLECTION,
                filters=[Filter(field="alias", op=FilterOp.EQ, value=alias.lower())],
            )
        )
        if existing:
            existing_id = existing[0].get("speaker_id", "")
            if existing_id != speaker_id:
                raise ValueError(f"Alias '{alias}' is already assigned to speaker '{existing_id}'")

        await storage.put(
            _ALIAS_COLLECTION,
            f"{speaker_id}:{alias.lower()}",
            {
                "speaker_id": speaker_id,
                "alias": alias.lower(),
                "display_alias": alias,
            },
        )
        logger.info("Alias '%s' assigned to speaker %s", alias, speaker_id)

    async def remove_alias(self, alias: str) -> None:
        """Remove an alias."""
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        results = await storage.query(
            Query(
                collection=_ALIAS_COLLECTION,
                filters=[Filter(field="alias", op=FilterOp.EQ, value=alias.lower())],
            )
        )
        for r in results:
            await storage.delete(_ALIAS_COLLECTION, r["_id"])
        logger.info("Alias '%s' removed", alias)

    async def resolve_speaker_name(self, name: str) -> str | None:
        """Resolve a speaker name or alias to a speaker_id. Returns None if not found.

        Prefers an **exact case** match on the speaker's name, so
        distinct speakers named e.g. "Garage" and "GARAGE" resolve
        to distinct ids rather than collapsing to whichever appears
        first in the list. Falls back to a case-insensitive match
        only when no exact match exists and exactly one speaker
        matches case-insensitively — ambiguous case-insensitive
        matches raise to force the caller to use the exact casing.

        Returns namespaced IDs (``<backend>:<native>``) consistent with
        ``list_speakers()`` so callers can feed the result to
        ``_route_id`` without a separate namespace-stamping step.
        """
        self._require_single_backend()  # raise if no backend configured
        speakers = await self.list_speakers()  # namespaced via service

        # 1) Exact case-sensitive name match — wins unambiguously
        # even when other speakers share the lowercased spelling.
        for s in speakers:
            if s.name == name:
                return s.speaker_id

        # 2) Case-insensitive name match — but only if it's unique.
        ci_matches = [s for s in speakers if s.name.lower() == name.lower()]
        if len(ci_matches) == 1:
            return ci_matches[0].speaker_id
        if len(ci_matches) > 1:
            names = sorted(s.name for s in ci_matches)
            raise KeyError(
                f"Ambiguous speaker name {name!r} — matches {names!r} "
                f"case-insensitively. Use the exact casing."
            )

        # 3) Alias lookup — aliases are stored lowercased.
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        results = await storage.query(
            Query(
                collection=_ALIAS_COLLECTION,
                filters=[Filter(field="alias", op=FilterOp.EQ, value=name.lower())],
            )
        )
        if results:
            sid = results[0].get("speaker_id")
            if sid is not None:
                sid_str = str(sid)
                if ":" in sid_str:
                    return sid_str
                # Legacy bare id — try to namespace it in-memory by scanning
                # all loaded backends.  This covers installs that haven't run
                # the 0001 migration yet, or where the migration couldn't
                # identify the backend at migration time.
                for backend_name, backend in self._backends.items():
                    try:
                        speakers = await backend.list_speakers()
                    except Exception:
                        continue
                    if any(s.speaker_id == sid_str for s in speakers):
                        return f"{backend_name}:{sid_str}"
                # Cannot namespace — return as-is; _route_id will raise
                # KeyError/ValueError later, which is the correct behaviour
                # (surfaces the un-migrated data clearly rather than silently
                # routing to the wrong backend).
                return sid_str
            return None

        return None

    async def resolve_speaker_names(self, names: list[str]) -> list[str]:
        """Resolve a list of speaker names/aliases to speaker_ids."""
        ids = []
        for name in names:
            sid = await self.resolve_speaker_name(name)
            if sid is None:
                raise KeyError(f"Unknown speaker or alias: {name!r}")
            ids.append(sid)
        return ids

    @staticmethod
    def _native_id(speaker_id: str) -> str:
        """Strip the ``<backend>:`` namespace prefix from a speaker ID.

        Pre-Task-6 shim: the service layer always works with namespaced IDs
        (``<backend>:<native>``), but the single-backend delegate still
        expects bare native IDs. ``_route_id`` in Task 6 will replace this
        with full backend dispatch; until then, strip and return the native
        part.

        Accepts bare IDs (no ``":"`` present) for backwards-compatibility
        with data that hasn't been migrated yet.
        """
        if ":" in speaker_id:
            _, _, native = speaker_id.partition(":")
            return native
        return speaker_id

    @staticmethod
    def _native_ids(speaker_ids: list[str]) -> list[str]:
        """Strip namespace prefix from a list of speaker IDs (see ``_native_id``)."""
        return [SpeakerService._native_id(sid) for sid in speaker_ids]

    def _route_id(self, speaker_id: str) -> tuple[SpeakerBackend, str]:
        """Split a namespaced speaker id and return ``(backend, native_id)``.

        Raises ``KeyError`` if the prefix names a backend that isn't loaded.
        Raises ``ValueError`` (via ``split_speaker_id``) if the id isn't
        namespaced.
        """
        backend_name, native_id = split_speaker_id(speaker_id)
        backend = self.backends.get(backend_name)
        if backend is None:
            raise KeyError(f"speaker backend {backend_name!r} not loaded")
        return backend, native_id

    def _route_ids(self, speaker_ids: list[str]) -> dict[str, list[str]]:
        """Group namespaced speaker ids by backend, returning ``{backend_name: [native_id, ...]}``.

        Raises ``KeyError`` if any id names a backend that isn't loaded.
        """
        grouped: dict[str, list[str]] = {}
        for sid in speaker_ids:
            backend_name, native_id = split_speaker_id(sid)
            if backend_name not in self.backends:
                raise KeyError(f"speaker backend {backend_name!r} not loaded")
            grouped.setdefault(backend_name, []).append(native_id)
        return grouped

    def _audio_url(self, file_path: str) -> str:
        """Build an HTTP URL for an output file so speakers can fetch it.

        Speakers need to access audio over HTTP — they can't read local files.
        We discover the LAN IP by connecting a UDP socket to an external address
        (no actual traffic is sent) which reveals the local interface IP.
        """
        from pathlib import Path

        from gilbert.core.output import OUTPUT_DIR

        # Resolve relative path under output dir
        rel = Path(file_path).relative_to(OUTPUT_DIR.resolve())
        host = self._web_host
        if host in ("0.0.0.0", "127.0.0.1", "localhost"):
            host = self._get_lan_ip()
        return f"http://{host}:{self._web_port}/output/{rel}"

    @staticmethod
    def _get_lan_ip() -> str:
        """Get the machine's LAN IP address."""
        import socket

        try:
            # Connect a UDP socket to a public address to discover the local interface.
            # No data is actually sent.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return str(s.getsockname()[0])
        except OSError:
            return "127.0.0.1"

    # ── Browser-echo fan-out ─────────────────────────────────────────

    async def _maybe_echo_to_browser(
        self,
        *,
        uri: str,
        volume: int | None,
        title: str,
        announce: bool,
        position_seconds: float | None,
        explicit_target_ids: list[str],
    ) -> None:
        """Mirror a primary play_uri to the caller's browser tab.

        Fires after the primary backend's ``play_uri`` when:
        - the caller has the ``speaker.browser_echo`` pref enabled,
        - the primary backend isn't ``browser`` (would double-play),
        - the caller's own browser is NOT already in the explicit target set (would double-play),
        - the event bus and users capability are both wired.

        Any error here is logged + swallowed: the primary play already
        succeeded and a glitchy secondary path shouldn't surface as
        user-visible failure.
        """
        if not await self._browser_echo_should_fire():
            return
        # Skip if the caller's own browser is in the explicit target set.
        from gilbert.interfaces.context import get_current_user
        user = get_current_user()
        if user and user.user_id:
            caller_browser_id = f"browser:{user.user_id}"
            if caller_browser_id in explicit_target_ids:
                return
        try:
            from gilbert.interfaces.context import (
                get_current_conversation_id,
                get_current_user,
            )
            from gilbert.interfaces.events import Event

            user = get_current_user()
            effective_volume = volume if volume is not None else 80
            effective_volume = max(0, min(100, int(effective_volume)))
            await self._event_bus_provider.bus.publish(
                Event(
                    event_type="speaker.browser.play",
                    data={
                        "user_id": user.user_id,
                        "conversation_id": get_current_conversation_id() or "",
                        "url": to_browser_url(uri),
                        "title": title,
                        "volume": effective_volume,
                        "announce": announce,
                        "position_seconds": position_seconds,
                    },
                    source="speaker.echo",
                )
            )
        except Exception:
            logger.debug("Browser-echo fan-out failed", exc_info=True)

    async def _maybe_echo_stop_to_browser(self) -> None:
        """Mirror a primary stop to the caller's browser tab.

        Same gating as the play variant. Browser-side handler pauses
        the auto-play element; per-clip ``<audio controls>`` history
        is left intact so the user can replay.
        """
        if not await self._browser_echo_should_fire():
            return
        try:
            from gilbert.interfaces.context import get_current_user
            from gilbert.interfaces.events import Event

            user = get_current_user()
            await self._event_bus_provider.bus.publish(
                Event(
                    event_type="speaker.browser.stop",
                    data={"user_id": user.user_id},
                    source="speaker.echo",
                )
            )
        except Exception:
            logger.debug("Browser-echo stop fan-out failed", exc_info=True)

    async def _browser_echo_should_fire(self) -> bool:
        """All preconditions for browser-echo fan-out in one place."""
        if self._event_bus_provider is None or self._users_svc is None:
            return False
        # Avoid double-play when the primary backend IS the browser —
        # the backend's own publish covers the user already.
        if self._backend_name == "browser":
            return False
        from gilbert.interfaces.context import get_current_user

        user = get_current_user()
        if not user.user_id or user.user_id == "system":
            return False
        try:
            value = await self._users_svc.get_user_pref(
                user.user_id, _BROWSER_ECHO_PREF_KEY, False
            )
        except Exception:
            logger.debug(
                "Browser-echo pref lookup failed for %s", user.user_id,
                exc_info=True,
            )
            return False
        return bool(value)

    async def _resolve_target_ids(
        self,
        speaker_names: list[str] | None,
    ) -> list[str]:
        """Resolve speaker names to IDs with fallback logic.

        Explicit names → resolve to IDs and cache.
        None → use last-used speakers.
        Last-used empty → use all speakers.
        """
        if speaker_names:
            ids = await self.resolve_speaker_names(speaker_names)
            self._last_speaker_ids = list(ids)
            return ids
        if self._last_speaker_ids:
            return list(self._last_speaker_ids)
        # Fall back to all speakers — use service-level list_speakers() so
        # the returned IDs are consistently namespaced.
        speakers = await self.list_speakers()
        return [s.speaker_id for s in speakers]

    async def prepare_speakers(self, speaker_ids: list[str]) -> None:
        """Ensure speakers are in the correct topology before playback.

        - Single speaker: unjoined from any group for solo playback.
        - Multiple speakers: grouped together.
        - Already correct: returns immediately.

        Backends that don't support grouping are skipped.
        """
        backend = self._require_single_backend()
        if not backend.supports_grouping or not speaker_ids:
            return

        native = self._native_ids(speaker_ids)
        if len(native) == 1:
            await backend.ungroup_speakers(native)
        else:
            await backend.group_speakers(native)

    async def group_speakers(self, speaker_ids: list[str]) -> None:
        """Group the given speakers. All must be on the same backend.

        Raises ``ValueError`` if speakers span multiple backends.
        """
        if not speaker_ids:
            return
        grouped = self._route_ids(speaker_ids)
        if len(grouped) > 1:
            names = ", ".join(speaker_ids[:3])
            backends_named = ", ".join(sorted(grouped))
            raise ValueError(
                f"Cannot group speakers across backends — {names} live on "
                f"different audio systems ({backends_named}) and can't be synchronized."
            )
        [(backend_name, native_ids)] = grouped.items()
        backend = self._backends[backend_name]
        await backend.group_speakers(native_ids)

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        """Remove speakers from their groups. All must be on the same backend.

        Raises ``ValueError`` if speakers span multiple backends.
        """
        if not speaker_ids:
            return
        grouped = self._route_ids(speaker_ids)
        if len(grouped) > 1:
            names = ", ".join(speaker_ids[:3])
            backends_named = ", ".join(sorted(grouped))
            raise ValueError(
                f"Cannot ungroup speakers across backends — {names} live on "
                f"different audio systems ({backends_named})."
            )
        [(backend_name, native_ids)] = grouped.items()
        backend = self._backends[backend_name]
        await backend.ungroup_speakers(native_ids)

    # --- Playback ---

    async def play_on_speakers(
        self,
        uri: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        title: str = "",
        position_seconds: float | None = None,
        didl_meta: str = "",
        announce: bool = False,
    ) -> None:
        """Play a URI on the specified speakers.

        Resolves names, prepares topology, then plays. ``didl_meta`` is
        an optional legacy DIDL-Lite envelope (unused by the aiosonos
        Sonos backend, still honoured by non-Sonos backends). When
        ``announce=True`` the speaker backend may route playback through
        a short-overlay path (e.g. Sonos ``audio_clip``) that ducks the
        current music, plays the clip, and auto-restores — no
        snapshot/restore ritual required.
        """
        target_ids = await self._resolve_target_ids(speaker_names)

        # Skip topology changes for announce clips — audio_clip is a
        # per-player overlay that doesn't need grouping, and reshuffling
        # the group mid-music can cause the speaker to lose its playback
        # queue, leaving the announcement URI as the "current" track
        # that loops after restore.
        if not announce:
            await self.prepare_speakers(target_ids)

        grouped = self._route_ids(target_ids)
        coros = []
        for backend_name, native_ids in grouped.items():
            backend = self.backends[backend_name]
            coros.append(
                backend.play_uri(
                    PlayRequest(
                        uri=uri,
                        speaker_ids=native_ids,
                        volume=volume,
                        title=title,
                        position_seconds=position_seconds,
                        didl_meta=didl_meta,
                        announce=announce,
                    )
                )
            )
        await asyncio.gather(*coros)

        # Optional second hop: fan the same playback out to the calling
        # user's connected browser tab if they've opted in. Runs after
        # the primary backend so a slow Sonos response doesn't delay the
        # browser. Failure to fan out is logged and swallowed — the
        # primary play already succeeded and the user shouldn't see an
        # error toast just because their secondary path glitched.
        await self._maybe_echo_to_browser(
            uri=uri,
            volume=volume,
            title=title,
            announce=announce,
            position_seconds=position_seconds,
            explicit_target_ids=target_ids,
        )

    async def enqueue_on_speakers(
        self,
        uri: str,
        speaker_names: list[str] | None = None,
        title: str = "",
        didl_meta: str = "",
    ) -> None:
        """Append a URI to the speaker's queue without stopping playback.

        Routes to ``SpeakerBackend.enqueue_uri``. Raises
        ``NotImplementedError`` if the backend doesn't support queueing —
        callers should guard on the music service's ``supports_queue``
        flag before invoking this.
        """
        target_ids = await self._resolve_target_ids(speaker_names)
        await self.prepare_speakers(target_ids)
        grouped = self._route_ids(target_ids)
        coros = []
        for backend_name, native_ids in grouped.items():
            backend = self.backends[backend_name]
            coros.append(
                backend.enqueue_uri(
                    PlayRequest(
                        uri=uri,
                        speaker_ids=native_ids,
                        title=title,
                        didl_meta=didl_meta,
                    )
                )
            )
        await asyncio.gather(*coros)

    async def play_queue_on_speakers(
        self,
        speaker_names: list[str] | None = None,
    ) -> bool:
        """Start or resume queue playback on the specified speakers.

        Does NOT clear the queue or add new content — callers should
        have built the queue via prior ``enqueue_on_speakers`` calls (or
        an equivalent). Raises ``NotImplementedError`` if the backend
        doesn't support queue operations.

        When playback is already in progress this is a no-op and returns
        ``False`` — otherwise the SMAPI SetAVTransportURI that normally
        precedes the Play action would reset the queue back to track 1,
        losing the listener's position mid-song. Returns ``True`` when
        a Play was actually issued.
        """
        self._require_single_backend()  # raise if no backend configured
        target_ids = await self._resolve_target_ids(speaker_names)
        await self.prepare_speakers(target_ids)

        # Check playback state on the coordinator (first target). All
        # group members share transport state, so any one is enough.
        if target_ids:
            try:
                routed_backend, native = self._route_id(target_ids[0])
                state = await routed_backend.get_playback_state(native)
            except Exception:
                state = PlaybackState.STOPPED
            if state == PlaybackState.PLAYING:
                return False

        grouped = self._route_ids(target_ids)
        coros = []
        for backend_name, native_ids in grouped.items():
            b = self.backends[backend_name]
            coros.append(b.play_queue(native_ids))
        await asyncio.gather(*coros)
        return True

    async def set_repeat_on_speakers(
        self,
        mode: LoopMode,
        speaker_names: list[str] | None = None,
    ) -> None:
        """Apply a queue repeat-mode to the given speakers.

        Resolves speaker names the same way ``play_on_speakers`` does
        (defaults to all speakers when none given) and forwards to the
        backend's ``set_repeat``. Backends that don't advertise
        ``supports_repeat`` raise ``NotImplementedError``; callers
        should guard on ``backend.supports_repeat`` first so the
        absence of support surfaces as a UI error rather than an
        exception.
        """
        self._require_single_backend()  # raise if no backend configured
        target_ids = await self._resolve_target_ids(speaker_names)
        grouped = self._route_ids(target_ids)
        coros = []
        for backend_name, native_ids in grouped.items():
            b = self.backends[backend_name]
            coros.append(b.set_repeat(mode, native_ids))
        await asyncio.gather(*coros)

    async def stop_speakers(
        self,
        speaker_names: list[str] | None = None,
    ) -> None:
        """Stop playback on the specified speakers."""
        target_ids = await self._resolve_target_ids(speaker_names)
        grouped = self._route_ids(target_ids)
        coros = []
        for backend_name, native_ids in grouped.items():
            b = self.backends[backend_name]
            coros.append(b.stop(native_ids))
        await asyncio.gather(*coros)
        await self._maybe_echo_stop_to_browser()

    async def get_now_playing(
        self,
        speaker_name: str | None = None,
    ) -> NowPlaying:
        """Return what's currently playing on a speaker.

        Speaker selection falls through in this order:

        1. If ``speaker_name`` is given, that speaker (resolved by name/alias).
        2. The first of the last-used speakers (typically the one music was
           last played on).
        3. The first speaker found whose state is ``PLAYING``.
        4. The first discovered speaker, regardless of state.

        Returns a ``NowPlaying`` with ``state=STOPPED`` if no speakers exist.
        """
        backend = self._require_single_backend()
        if speaker_name:
            sid = await self.resolve_speaker_name(speaker_name)
            if sid is None:
                raise KeyError(f"Unknown speaker or alias: {speaker_name!r}")
            routed_backend, native = self._route_id(sid)
            return await routed_backend.get_now_playing(native)

        if self._last_speaker_ids:
            return await backend.get_now_playing(self._native_id(self._last_speaker_ids[0]))

        speakers = await self.list_speakers()
        if not speakers:
            return NowPlaying(state=PlaybackState.STOPPED)
        for s in speakers:
            if s.state == PlaybackState.PLAYING:
                return await backend.get_now_playing(self._native_id(s.speaker_id))
        return await backend.get_now_playing(self._native_id(speakers[0].speaker_id))

    # --- Announce ---

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        context: str = "",
    ) -> str:
        """Announce text over speakers using TTS.

        If no speaker_names are given, falls back to the configured
        default announce speakers (or all speakers if that's also empty).

        Announcements are serialized **per speaker**, not globally —
        two concurrent announcements that target disjoint speaker sets
        fan out and run in parallel, while any overlap on a shared
        speaker queues on that speaker's lock. This lets the assistant
        say different things on different speakers at the same time
        (e.g. personal greetings per room) without letting two clips
        collide on the same device's snapshot/restore state.
        """
        # Fall back to configured default speakers
        if speaker_names is None and self._default_announce_speakers:
            speaker_names = self._default_announce_speakers
        # Resolve target speakers outside any per-speaker lock so the
        # lock set is known before we start acquiring. An empty set
        # means no speakers to announce on — let _announce_inner handle
        # the degenerate case without acquiring any locks.
        target_ids = await self._resolve_target_ids(speaker_names)
        if not target_ids:
            return await self._announce_inner(
                text, speaker_names, volume, target_ids=target_ids, context=context
            )
        locks = await self._get_speaker_locks(target_ids)
        async with contextlib.AsyncExitStack() as stack:
            for lock in locks:
                await stack.enter_async_context(lock)
            return await self._announce_inner(
                text, speaker_names, volume, target_ids=target_ids, context=context
            )

    async def _get_speaker_locks(
        self, speaker_ids: list[str]
    ) -> list[asyncio.Lock]:
        """Return one lock per unique speaker ID, ordered by ID.

        Sorted-order acquisition is the standard fix for the
        multi-lock deadlock: if caller A asks for [s1, s2] and caller
        B asks for [s2, s3], both acquire them in the same global
        order so neither can end up holding a lock the other needs in
        reverse. Locks are created lazily under ``_speaker_locks_guard``
        so the get-or-create step itself is race-free.
        """
        unique_ids = sorted(set(speaker_ids))
        async with self._speaker_locks_guard:
            for sid in unique_ids:
                if sid not in self._speaker_locks:
                    self._speaker_locks[sid] = asyncio.Lock()
        return [self._speaker_locks[sid] for sid in unique_ids]

    async def _announce_inner(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        target_ids: list[str] | None = None,
        context: str = "",
    ) -> str:
        """Inner announce — must be called with the target speakers'
        per-speaker locks already held. Accepts a pre-resolved
        ``target_ids`` to avoid re-running name resolution (and the
        ``_last_speaker_ids`` mutation that goes with it) after the
        caller has already done so to build the lock set.
        """
        if self._tts_svc is None:
            raise RuntimeError("TTS service is not available — cannot announce")

        from gilbert.interfaces.tts import AudioFormat, SynthesisRequest, TTSProvider

        if not isinstance(self._tts_svc, TTSProvider):
            raise TypeError("Expected TTSService for text_to_speech capability")

        backend = self._require_single_backend()

        # Generate TTS audio
        request = SynthesisRequest(
            text=text,
            voice_id="",
            output_format=AudioFormat.MP3,
            context=context,
        )
        result = await self._tts_svc.synthesize(request)

        # Save to a file so the speaker can access it via URI
        output_dir = get_output_dir("speaker")
        cleanup_old_files(output_dir, self._output_ttl_seconds)
        file_path = output_dir / f"announce-{uuid.uuid4()}.mp3"
        file_path.write_bytes(result.audio)

        # Determine volume
        effective_volume = volume or self._default_announce_volume

        # Snapshot current playback state so we can resume after —
        # on the aiosonos Sonos backend snapshot/restore are no-ops
        # because ``audio_clip`` (triggered by ``announce=True`` below)
        # ducks + auto-restores natively. Kept in the flow so non-
        # Sonos backends that implement snapshot/restore still work.
        if target_ids is None:
            target_ids = await self._resolve_target_ids(speaker_names)
        await backend.snapshot(self._native_ids(target_ids))

        # Play on speakers — topology handled by play_on_speakers.
        # ``announce=True`` tells backends that support it (Sonos) to
        # route through a short-overlay clip path so the listener
        # doesn't hear music stop completely, and the previous track
        # resumes automatically when the clip ends.
        audio_url = self._audio_url(str(file_path.resolve()))
        await self.play_on_speakers(
            uri=audio_url,
            speaker_names=speaker_names,
            volume=effective_volume,
            title=f"Announcement: {text[:50]}",
            announce=True,
        )

        # Wait for playback to finish before restoring.
        # Use audio duration if available, fall back to polling.
        # On audio_clip-capable backends this is just a courtesy wait
        # before releasing the announce lock — the speaker has already
        # scheduled its own restore once the clip ends.
        duration = self._estimate_mp3_duration(result.audio)
        if duration > 0:
            await asyncio.sleep(duration + 0.5)
        else:
            await self._wait_for_playback(target_ids)

        # Restore previous playback state (no-op on aiosonos; kept for
        # non-Sonos backends that still need the manual restore).
        try:
            await backend.restore(self._native_ids(target_ids))
        except Exception:
            logger.debug("Failed to restore playback after announcement")

        return str(file_path)

    @staticmethod
    def _estimate_mp3_duration(audio_data: bytes) -> float:
        """Estimate MP3 duration from file size and bitrate.

        Parses the first MP3 frame header to get the bitrate, then
        calculates duration = size / (bitrate / 8). Returns 0 on failure.
        """
        try:
            # Find first MP3 frame sync (0xFF 0xFB/0xFA/0xF3/0xF2)
            for i in range(min(len(audio_data) - 1, 4096)):
                if audio_data[i] == 0xFF and (audio_data[i + 1] & 0xE0) == 0xE0:
                    header = audio_data[i : i + 4]
                    if len(header) < 4:
                        return 0
                    # MPEG version, layer, bitrate index
                    version = (header[1] >> 3) & 0x03
                    layer = (header[1] >> 1) & 0x03
                    br_idx = (header[2] >> 4) & 0x0F
                    # MPEG1 Layer3 bitrate table
                    if version == 3 and layer == 1 and 1 <= br_idx <= 14:
                        bitrates = [
                            0,
                            32,
                            40,
                            48,
                            56,
                            64,
                            80,
                            96,
                            112,
                            128,
                            160,
                            192,
                            224,
                            256,
                            320,
                        ]
                        kbps = bitrates[br_idx]
                        return len(audio_data) / (kbps * 125)
            return 0
        except Exception:
            return 0

    async def _wait_for_playback(
        self,
        speaker_ids: list[str],
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Poll speaker state until playback finishes or times out."""
        from gilbert.interfaces.speaker import PlaybackState

        if not speaker_ids:
            return

        # Use the first speaker (coordinator) to check state
        target_id = speaker_ids[0]
        elapsed = 0.0

        # Wait briefly for playback to start (TRANSITIONING → PLAYING)
        await asyncio.sleep(0.5)
        elapsed += 0.5

        while elapsed < timeout:
            try:
                routed_backend, native = self._route_id(target_id)
                state = await routed_backend.get_playback_state(native)
                if state not in (PlaybackState.PLAYING, PlaybackState.TRANSITIONING):
                    return
            except Exception:
                return  # Can't check state — don't block forever

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    # --- WsHandlerProvider protocol ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {"speaker.info": self._ws_speaker_info}

    async def _ws_speaker_info(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Report whether the service is enabled and which backend is primary.

        Lets the SPA reason about the speaker subsystem without poking
        at the settings RPC (which is admin-only and heavier). The
        Browser Echo toggle reads this to disable itself when the
        primary backend is already ``browser`` — flipping the echo
        toggle in that config would be a no-op (the gate in
        ``_browser_echo_should_fire`` short-circuits to avoid
        double-play), so the UI shouldn't pretend otherwise.

        Public — any authenticated connection can read.
        """
        return {
            "type": "gilbert.result",
            "ref": frame.get("id"),
            "enabled": self._enabled,
            "backend": self._backend_name if self._enabled else "",
        }

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "speaker"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        tools = [
            ToolDefinition(
                name="list_speakers",
                slash_group="speaker",
                slash_command="list",
                slash_help="List all speakers with state + volume: /speaker list",
                description="List all discovered speakers with their current state, volume, and group info.",
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="play_audio",
                slash_group="speaker",
                slash_command="play",
                slash_help="Play a URI on speakers: /speaker play <uri> [speakers] [volume]",
                description=(
                    "Play audio from an HTTP(S) URI on one or more speakers. "
                    "The speaker fetches the bytes over the network — it CANNOT "
                    "read local file paths or workspace-relative paths. "
                    "If you want to play a workspace file (something in "
                    "``uploads/``, ``outputs/``, or ``scratch/``), first call "
                    "``share_workspace_file`` to mint an HTTP URL for it, then "
                    "pass that URL's ``url`` field here as ``uri``. Passing a "
                    "raw path like ``uploads/song.mp3`` will fail."
                ),
                parameters=[
                    ToolParameter(
                        name="uri",
                        type=ToolParameterType.STRING,
                        description=(
                            "HTTP(S) URL of the audio to play. For workspace "
                            "files, get a URL from ``share_workspace_file`` "
                            "first — local paths will not work."
                        ),
                    ),
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, uses last-used speakers or all.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100). If omitted, uses current volume.",
                        required=False,
                    ),
                    ToolParameter(
                        name="position_seconds",
                        type=ToolParameterType.NUMBER,
                        description="Start playback at this position in seconds.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="stop_audio",
                slash_group="speaker",
                slash_command="stop",
                slash_help="Stop playback: /speaker stop [speakers]",
                description="Stop playback on speakers.",
                parameters=[
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, stops all.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="set_volume",
                slash_group="speaker",
                slash_command="volume",
                slash_help="Set speaker volume: /speaker volume <speaker> <0-100>",
                description="Set volume on a speaker.",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Speaker name or alias.",
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100).",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="get_volume",
                slash_group="speaker",
                slash_command="get_volume",
                slash_help="Read speaker volume: /speaker get_volume <speaker>",
                description="Get the current volume of a speaker.",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Speaker name or alias.",
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="set_speaker_alias",
                slash_group="speaker",
                slash_command="alias",
                slash_help="Alias a speaker: /speaker alias <speaker> <alias>",
                description="Assign an alias name to a speaker (e.g., 'Living Room Speaker' for 'Speaker 2'). Admin only.",
                required_role="admin",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Current speaker name or ID.",
                    ),
                    ToolParameter(
                        name="alias",
                        type=ToolParameterType.STRING,
                        description="The alias name to assign.",
                    ),
                ],
            ),
            ToolDefinition(
                name="remove_speaker_alias",
                slash_group="speaker",
                slash_command="unalias",
                slash_help="Remove a speaker alias: /speaker unalias <alias>",
                description="Remove an alias from a speaker. Admin only.",
                required_role="admin",
                parameters=[
                    ToolParameter(
                        name="alias",
                        type=ToolParameterType.STRING,
                        description="The alias to remove.",
                    ),
                ],
            ),
            ToolDefinition(
                name="announce",
                slash_group="speaker",
                slash_command="announce",
                slash_help=(
                    'Speak text on speakers via TTS: /speaker announce "<text>" [speakers] [volume]'
                ),
                description=(
                    "Announce a message over speakers using text-to-speech. "
                    "This is the primary tool for speaking text out loud — it handles everything: "
                    "generates audio via TTS, groups speakers if needed, sets volume, and plays. "
                    "If no speakers specified, uses last-used speakers or all. "
                    "Use this instead of 'speak' when you want audio played on speakers."
                ),
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="The text to announce.",
                    ),
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, uses last-used speakers or all.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100) for the announcement.",
                        required=False,
                    ),
                    ToolParameter(
                        name="context",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional one-line description of the situation "
                            "or mood (e.g. 'cheery good-morning greeting', "
                            "'urgent shop alert', 'sarcastic reply'). "
                            "Helps the TTS engine pick expressive delivery — "
                            "ignored by backends that don't support it."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
                # Safe to run in parallel with other announces targeting
                # *different* speakers — per-speaker locks in the service
                # still serialize same-speaker collisions so clips never
                # overlap on one device.
                parallel_safe=True,
            ),
        ]

        # Add grouping tools if the backend supports it
        if self._backends and self._require_single_backend().supports_grouping:
            tools.extend(
                [
                    ToolDefinition(
                        name="list_speaker_groups",
                        slash_group="speaker",
                        slash_command="groups",
                        slash_help="List speaker groups: /speaker groups",
                        description="List current speaker groups.",
                        required_role="user",
                    ),
                    ToolDefinition(
                        name="group_speakers",
                        slash_group="speaker",
                        slash_command="group",
                        slash_help="Group speakers for sync playback: /speaker group <s1>,<s2>",
                        description="Group speakers together for synchronized playback.",
                        parameters=[
                            ToolParameter(
                                name="speakers",
                                type=ToolParameterType.ARRAY,
                                description="Speaker names or aliases to group together (at least 2).",
                            ),
                        ],
                        required_role="user",
                    ),
                    ToolDefinition(
                        name="ungroup_speakers",
                        slash_group="speaker",
                        slash_command="ungroup",
                        slash_help="Remove speakers from groups: /speaker ungroup <s1>,<s2>",
                        description="Remove speakers from their groups.",
                        parameters=[
                            ToolParameter(
                                name="speakers",
                                type=ToolParameterType.ARRAY,
                                description="Speaker names or aliases to ungroup.",
                            ),
                        ],
                        required_role="user",
                    ),
                ]
            )

        return tools

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "list_speakers":
                return await self._tool_list_speakers()
            case "play_audio":
                return await self._tool_play_audio(arguments)
            case "stop_audio":
                return await self._tool_stop_audio(arguments)
            case "set_volume":
                return await self._tool_set_volume(arguments)
            case "get_volume":
                return await self._tool_get_volume(arguments)
            case "set_speaker_alias":
                return await self._tool_set_alias(arguments)
            case "remove_speaker_alias":
                return await self._tool_remove_alias(arguments)
            case "announce":
                return await self._tool_announce(arguments)
            case "list_speaker_groups":
                return await self._tool_list_groups()
            case "group_speakers":
                return await self._tool_group_speakers(arguments)
            case "ungroup_speakers":
                return await self._tool_ungroup_speakers(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_list_speakers(self) -> str:
        speakers = await self._require_single_backend().list_speakers()

        # Enrich with aliases
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Query

        all_aliases = await storage.query(Query(collection=_ALIAS_COLLECTION))
        alias_map: dict[str, list[str]] = {}
        for a in all_aliases:
            sid = a.get("speaker_id", "")
            alias_map.setdefault(sid, []).append(a.get("display_alias", ""))

        result = []
        for s in speakers:
            entry: dict[str, Any] = {
                "speaker_id": s.speaker_id,
                "name": s.name,
                "ip_address": s.ip_address,
                "model": s.model,
                "volume": s.volume,
                "state": s.state.value,
                "group_name": s.group_name,
                "is_group_coordinator": s.is_group_coordinator,
            }
            aliases = alias_map.get(s.speaker_id)
            if aliases:
                entry["aliases"] = aliases
            result.append(entry)

        return json.dumps(result)

    async def _tool_play_audio(self, arguments: dict[str, Any]) -> str:
        uri = arguments["uri"]
        speaker_names: list[str] = arguments.get("speakers", [])
        volume: int | None = arguments.get("volume")
        position: float | None = arguments.get("position_seconds")

        await self.play_on_speakers(
            uri=uri,
            speaker_names=speaker_names or None,
            volume=volume,
            position_seconds=position,
        )
        return json.dumps({"status": "playing", "uri": uri})

    async def _tool_stop_audio(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments.get("speakers", [])
        await self.stop_speakers(speaker_names or None)
        return json.dumps({"status": "stopped"})

    async def _tool_set_volume(self, arguments: dict[str, Any]) -> str:
        name = arguments["speaker"]
        volume = arguments["volume"]
        sid = await self.resolve_speaker_name(name)
        if sid is None:
            return json.dumps({"error": f"Speaker not found: {name}"})
        backend, native = self._route_id(sid)
        await backend.set_volume(native, volume)
        return json.dumps({"status": "ok", "speaker": name, "volume": volume})

    async def _tool_get_volume(self, arguments: dict[str, Any]) -> str:
        name = arguments["speaker"]
        sid = await self.resolve_speaker_name(name)
        if sid is None:
            return json.dumps({"error": f"Speaker not found: {name}"})
        backend, native = self._route_id(sid)
        volume = await backend.get_volume(native)
        return json.dumps({"speaker": name, "volume": volume})

    async def _tool_set_alias(self, arguments: dict[str, Any]) -> str:
        speaker_name = arguments["speaker"]
        alias = arguments["alias"]

        sid = await self.resolve_speaker_name(speaker_name)
        if sid is None:
            return json.dumps({"error": f"Speaker not found: {speaker_name}"})

        try:
            await self.set_alias(sid, alias)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        return json.dumps({"status": "ok", "speaker": speaker_name, "alias": alias})

    async def _tool_remove_alias(self, arguments: dict[str, Any]) -> str:
        alias = arguments["alias"]
        await self.remove_alias(alias)
        return json.dumps({"status": "ok", "alias": alias})

    async def _tool_announce(self, arguments: dict[str, Any]) -> str:
        text = arguments["text"]
        speaker_names: list[str] = arguments.get("speakers", [])
        volume: int | None = arguments.get("volume")
        context: str = arguments.get("context", "") or ""

        try:
            file_path = await self.announce(
                text=text,
                speaker_names=speaker_names or None,
                volume=volume,
                context=context,
            )
        except RuntimeError as e:
            return json.dumps({"error": str(e)})

        return json.dumps(
            {
                "status": "announced",
                "text": text,
                "audio_file": file_path,
            }
        )

    async def _tool_list_groups(self) -> str:
        groups = await self.list_speaker_groups()
        return json.dumps(
            [
                {
                    "group_id": g.group_id,
                    "name": g.name,
                    "coordinator_id": g.coordinator_id,
                    "member_ids": g.member_ids,
                }
                for g in groups
            ]
        )

    async def _tool_group_speakers(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments["speakers"]
        speaker_ids = await self.resolve_speaker_names(speaker_names)

        try:
            await self.group_speakers(speaker_ids)
            # Fetch the group info for the response
            groups = await self.list_speaker_groups()
            # Find the group containing the first speaker (simplistic but works)
            target_group = None
            if groups:
                target_group = groups[0]  # Most recently formed group
        except ValueError as e:
            return json.dumps({"error": str(e)})

        if target_group:
            return json.dumps(
                {
                    "status": "grouped",
                    "group_id": target_group.group_id,
                    "name": target_group.name,
                    "member_ids": target_group.member_ids,
                }
            )
        return json.dumps({"status": "grouped"})

    async def _tool_ungroup_speakers(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments["speakers"]
        speaker_ids = await self.resolve_speaker_names(speaker_names)
        try:
            await self.ungroup_speakers(speaker_ids)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"status": "ungrouped"})
