"""Screen service — remote display screens for documents, text, and images.

Manages browser-based display screens that can be controlled by AI tools.
Users open /screens on any device, name it, and it becomes a target for
content. AI tools push documents, text, or images to named screens.

Uses Server-Sent Events (SSE) for push delivery to passive displays.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Keepalive interval — SSE comment to prevent proxy/browser timeout
_KEEPALIVE_SECONDS = 15

# Sentinel pushed to a screen's queue when it's replaced or disconnected
_SENTINEL = ""

# Words people append to screen names when speaking naturally
_SCREEN_SUFFIXES = {"screen", "tv", "display", "monitor", "panel"}

# Supported image extensions
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}

# MIME types for temp file serving
_MIME_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
}


@dataclass
class ConnectedScreen:
    """A browser tab connected as a display screen."""

    name: str
    key: str  # normalized lowercase key
    default_url: str | None = None
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)


@dataclass
class TempFile:
    """A temporary file managed by the screen service."""

    token: str
    path: Path
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _normalize(name: str) -> str:
    """Normalize a screen name to a lowercase key."""
    return name.strip().lower()


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """Format a Server-Sent Event message."""
    payload = json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


def _strip_screen_suffix(name: str) -> str:
    """Strip common screen-related suffixes and possessives.

    "shop screen" -> "shop", "brian's tv" -> "brian"
    """
    words = name.strip().lower().split()
    while words and words[-1] in _SCREEN_SUFFIXES:
        words.pop()
    result = " ".join(words)
    if result.endswith("\u2019s"):
        result = result[:-2]
    elif result.endswith("'s"):
        result = result[:-2]
    return result.strip()


class ScreenService(Service):
    """Manages connected display screens and provides AI tools for pushing content.

    Capabilities: screen_display, ai_tools
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._screens: dict[str, ConnectedScreen] = {}
        self._tmp_dir: Path | None = None
        self._files: dict[str, TempFile] = {}
        self._ttl: int = 1800
        self._cleanup_interval: int = 300
        self._event_bus_svc: Any = None  # EventBusService
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="screens",
            capabilities=frozenset({"screen_display", "ai_tools", "ws_handlers"}),
            requires=frozenset(),
            optional=frozenset({"knowledge", "scheduler", "event_bus", "configuration"}),
            events=frozenset({"screen.connected", "screen.disconnected"}),
            toggleable=True,
            toggle_description="Remote display screens",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Check enabled
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                if not section.get("enabled", False):
                    logger.info("Screen service disabled")
                    return

        self._enabled = True
        self._event_bus_svc = resolver.get_capability("event_bus")

        # Load config
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                self._ttl = int(section.get("tmp_ttl_seconds", 1800))
                self._cleanup_interval = int(section.get("cleanup_interval_seconds", 300))

        # Create temp directory
        self._tmp_dir = Path(".gilbert/output/screens")
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        # Register periodic cleanup with scheduler
        scheduler = resolver.get_capability("scheduler")
        if scheduler is not None:
            from gilbert.interfaces.scheduler import Schedule

            scheduler.add_job(
                "screen-tmp-cleanup",
                Schedule.every(self._cleanup_interval),
                self._cleanup_expired,
            )

        logger.info("Screen service started")

    async def stop(self) -> None:
        self._cleanup_all()

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "screens"

    @property
    def config_category(self) -> str:
        return "Infrastructure"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="tmp_ttl_seconds", type=ToolParameterType.INTEGER,
                description="Time-to-live for temporary files (seconds).",
                default=1800,
            ),
            ConfigParam(
                key="cleanup_interval_seconds", type=ToolParameterType.INTEGER,
                description="Interval between cleanup runs (seconds).",
                default=300,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._ttl = int(config.get("tmp_ttl_seconds", self._ttl))
        self._cleanup_interval = int(config.get("cleanup_interval_seconds", self._cleanup_interval))

    @property
    def _knowledge(self) -> Any:
        """Lazily resolve KnowledgeService — it may start after us."""
        if self._resolver is None:
            return None
        return self._resolver.get_capability("knowledge")

    # ── Screen Registry ─────────────────────────────────────────

    def connect(self, name: str, default_url: str | None = None) -> ConnectedScreen:
        """Register a screen. Replaces any existing screen with the same key."""
        key = _normalize(name)
        existing = self._screens.get(key)
        if existing:
            existing.queue.put_nowait(_SENTINEL)
            logger.info("Screen replaced: %s", name)

        screen = ConnectedScreen(
            name=name.strip(), key=key, default_url=default_url or None
        )
        self._screens[key] = screen
        logger.info("Screen connected: %s (key=%s)", screen.name, key)

        if self._event_bus_svc is not None:
            asyncio.ensure_future(
                self._event_bus_svc.bus.publish(Event(
                    event_type="screen.connected",
                    data={"name": screen.name, "key": key},
                    source="screens",
                ))
            )
        return screen

    def disconnect(self, key: str, screen: ConnectedScreen) -> None:
        """Remove a screen. Only removes if it's the same object (race safety)."""
        current = self._screens.get(key)
        if current is screen:
            del self._screens[key]
            logger.info("Screen disconnected: %s", screen.name)

            if self._event_bus_svc is not None:
                asyncio.ensure_future(
                    self._event_bus_svc.bus.publish(Event(
                        event_type="screen.disconnected",
                        data={"name": screen.name, "key": key},
                        source="screens",
                    ))
                )

    def list_screens(self) -> list[dict[str, Any]]:
        """Return info about all connected screens."""
        result = []
        for s in self._screens.values():
            info: dict[str, Any] = {
                "name": s.name,
                "key": s.key,
                "connected_at": s.connected_at.isoformat(),
            }
            if s.default_url:
                info["default_url"] = s.default_url
            result.append(info)
        return result

    def get_screen(self, name: str) -> ConnectedScreen | None:
        """Look up a screen by normalized name."""
        return self._screens.get(_normalize(name))

    # ── SSE Event Stream ────────────────────────────────────────

    async def event_stream(self, screen: ConnectedScreen) -> AsyncGenerator[str, None]:
        """SSE generator — yields events and keepalive comments."""
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        screen.queue.get(), timeout=_KEEPALIVE_SECONDS
                    )
                    if msg == _SENTINEL:
                        return
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self.disconnect(screen.key, screen)

    # ── Push Methods ────────────────────────────────────────────

    def push_document(
        self,
        screen_name: str,
        title: str,
        serve_url: str,
        content_type: str = "pdf",
        tmp_token: str | None = None,
    ) -> bool:
        """Push a document display event to a screen."""
        screen = self.get_screen(screen_name)
        if not screen:
            return False

        data: dict[str, Any] = {
            "type": "show_document",
            "title": title,
            "content_type": content_type,
            "serve_url": serve_url,
        }
        if tmp_token:
            data["tmp_token"] = tmp_token

        screen.queue.put_nowait(_sse_event("show_document", data))
        logger.info("Screen push document: %s -> %s", title, screen.name)
        return True

    def push_text(self, screen_name: str, title: str, content: str) -> bool:
        """Push text/markdown content to a screen."""
        screen = self.get_screen(screen_name)
        if not screen:
            return False

        screen.queue.put_nowait(_sse_event("show_text", {
            "type": "show_text",
            "title": title,
            "content": content,
        }))
        logger.info("Screen push text: %s -> %s", title, screen.name)
        return True

    def push_images(
        self, screen_name: str, title: str, images: list[dict[str, Any]]
    ) -> bool:
        """Push an image gallery event to a screen."""
        screen = self.get_screen(screen_name)
        if not screen:
            return False

        screen.queue.put_nowait(_sse_event("show_images", {
            "type": "show_images",
            "title": title,
            "images": images,
        }))
        logger.info("Screen push images: %s -> %s (%d images)", title, screen.name, len(images))
        return True

    def push_clear(self, screen_name: str) -> bool:
        """Push a clear event to a screen."""
        screen = self.get_screen(screen_name)
        if not screen:
            return False

        data: dict[str, Any] = {"type": "clear"}
        if screen.default_url:
            data["default_url"] = screen.default_url
        screen.queue.put_nowait(_sse_event("clear", data))
        logger.info("Screen push clear: %s", screen.name)
        return True

    def push_loading(self, screen_name: str, message: str = "Loading...") -> bool:
        """Push a loading indicator to a screen."""
        screen = self.get_screen(screen_name)
        if not screen:
            return False

        screen.queue.put_nowait(_sse_event("loading", {"type": "loading", "message": message}))
        return True

    def push_error(self, screen_name: str, message: str) -> bool:
        """Push an error message to a screen (auto-dismisses)."""
        screen = self.get_screen(screen_name)
        if not screen:
            return False

        screen.queue.put_nowait(_sse_event("error", {"type": "error", "message": message}))
        logger.info("Screen push error: %s -> %s", message, screen.name)
        return True

    # ── Temp File Management ────────────────────────────────────

    def extract_pages(self, pdf_bytes: bytes, pages: list[int]) -> str:
        """Extract specific pages from a PDF into a temp file. Returns token."""
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = len(reader.pages)
        out_of_range = [p for p in pages if p < 1 or p > total]
        if out_of_range:
            raise ValueError(
                f"Pages {out_of_range} out of range (document has {total} pages)"
            )

        writer = PdfWriter()
        for page_num in pages:
            writer.add_page(reader.pages[page_num - 1])

        token = uuid.uuid4().hex
        assert self._tmp_dir is not None
        tmp_path = self._tmp_dir / f"{token}.pdf"
        with open(tmp_path, "wb") as f:
            writer.write(f)

        self._files[token] = TempFile(token=token, path=tmp_path)
        logger.info("Extracted pages %s to temp file %s", sorted(pages), token)
        return token

    def save_temp_file(self, filename: str, data: bytes) -> str:
        """Save arbitrary bytes to a temp file. Returns token."""
        token = uuid.uuid4().hex
        ext = Path(filename).suffix.lower() or ".bin"
        assert self._tmp_dir is not None
        tmp_path = self._tmp_dir / f"{token}{ext}"
        tmp_path.write_bytes(data)
        self._files[token] = TempFile(token=token, path=tmp_path)
        return token

    def get_temp_path(self, token: str) -> Path | None:
        """Get the file path for a token, or None if expired/missing."""
        entry = self._files.get(token)
        if not entry:
            return None
        age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
        if age > self._ttl:
            self._remove_file(token)
            return None
        if not entry.path.exists():
            del self._files[token]
            return None
        return entry.path

    def get_temp_mime_type(self, token: str) -> str:
        """Get the MIME type for a temp file."""
        entry = self._files.get(token)
        if not entry:
            return "application/octet-stream"
        ext = entry.path.suffix.lower()
        return _MIME_TYPES.get(ext, "application/octet-stream")

    async def _cleanup_expired(self) -> None:
        """Remove expired temp files."""
        now = datetime.now(timezone.utc)
        expired = [
            token
            for token, entry in self._files.items()
            if (now - entry.created_at).total_seconds() > self._ttl
        ]
        for token in expired:
            self._remove_file(token)
        if expired:
            logger.info("Screen temp cleanup: removed %d files", len(expired))

    def _cleanup_all(self) -> None:
        """Remove all temp files (shutdown)."""
        tokens = list(self._files.keys())
        for token in tokens:
            self._remove_file(token)
        if self._tmp_dir and self._tmp_dir.exists():
            for f in self._tmp_dir.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass

    def _remove_file(self, token: str) -> None:
        """Remove a single temp file by token."""
        entry = self._files.pop(token, None)
        if entry and entry.path.exists():
            try:
                entry.path.unlink()
            except OSError:
                pass

    # ── Screen Name Resolution ──────────────────────────────────

    def resolve_screen(self, screen_name: str) -> tuple[str, str | None]:
        """Resolve a screen name with fuzzy matching.

        Returns (resolved_name, None) on success or ("", error) on failure.
        """
        screens = self.list_screens()
        if not screens:
            return "", "No screens are connected. Open /screens in a browser to set one up."

        names = [s["name"] for s in screens]
        query = screen_name.strip().lower()

        # 1. Exact match on key
        for s in screens:
            if s["key"] == query:
                return s["name"], None

        # 2. Strip suffix and try exact match
        stripped = _strip_screen_suffix(query)
        if stripped:
            for s in screens:
                if s["key"] == stripped:
                    return s["name"], None
                if _strip_screen_suffix(s["key"]) == stripped:
                    return s["name"], None

        # 3. Fuzzy match
        candidates: dict[str, str] = {}
        for n in names:
            candidates[n.lower()] = n
            stripped_n = _strip_screen_suffix(n.lower())
            if stripped_n and stripped_n != n.lower():
                candidates[stripped_n] = n

        candidate_list = list(candidates.keys())
        for q in (query, stripped):
            if not q:
                continue
            matches = difflib.get_close_matches(q, candidate_list, n=1, cutoff=0.6)
            if matches:
                return candidates[matches[0]], None

        listing = ", ".join(f'"{n}"' for n in names)
        return "", f'Screen "{screen_name}" not found. Connected screens: {listing}.'

    # ── Page Selection ────────────────────────────────────────

    @staticmethod
    def _find_pages_by_keyword(pdf_data: bytes, query: str) -> list[int] | None:
        """Find pages in a PDF that contain the query terms.

        Uses TF-IDF-style scoring: terms that appear on many pages
        (like document title in headers) are weighted low. Terms that
        appear on only a few pages (the actual content) are weighted high.
        """
        try:
            from pypdf import PdfReader
        except ImportError:
            return None

        try:
            reader = PdfReader(io.BytesIO(pdf_data))
        except Exception:
            return None

        total_pages = len(reader.pages)
        if not total_pages:
            return None

        terms = [t.lower() for t in query.split() if len(t) >= 3]
        if not terms:
            return None

        # Extract text from all pages
        page_texts: list[str] = []
        for page in reader.pages:
            page_texts.append((page.extract_text() or "").lower())

        # Count how many pages each term appears on (document frequency)
        term_doc_freq: dict[str, int] = {}
        for t in terms:
            term_doc_freq[t] = sum(1 for text in page_texts if t in text)

        # Score each page: terms that appear on fewer pages get higher weight
        # Weight = 1 / (fraction of pages containing the term)
        # A term on 1 of 12 pages gets weight 12, a term on all 12 gets weight 1
        page_scores: list[tuple[int, float]] = []
        for i, text in enumerate(page_texts):
            if not text.strip():
                continue
            score = 0.0
            for t in terms:
                if t in text:
                    df = term_doc_freq[t]
                    if df > 0:
                        score += total_pages / df
            if score > 0:
                page_scores.append((i + 1, score))

        if not page_scores:
            return None

        # Sort by score descending — pages with rare/distinctive terms rank highest
        page_scores.sort(key=lambda x: x[1], reverse=True)
        best_score = page_scores[0][1]

        # Take pages scoring within 60% of the best
        threshold = best_score * 0.6
        core_pages: set[int] = set()
        for page_num, score in page_scores:
            if score >= threshold:
                core_pages.add(page_num)

        # Expand by +/- 1 for context
        expanded: set[int] = set()
        for p in core_pages:
            expanded.add(max(1, p - 1))
            expanded.add(p)
            expanded.add(min(total_pages, p + 1))

        page_list = sorted(expanded)
        if len(page_list) > 12:
            page_list = page_list[:12]

        return page_list

    @staticmethod
    def _find_pages_by_keyword_from_text(text: str, query: str) -> list[int] | None:
        """Find relevant pages from cached extracted text with [Page N] markers.

        Same TF-IDF scoring as _find_pages_by_keyword but works on pre-extracted
        text (which includes OCR and Vision enrichment).
        """
        import re

        terms = [t.lower() for t in query.split() if len(t) >= 3]
        if not terms:
            return None

        # Split text into pages using [Page N] markers
        page_pattern = re.compile(r"\[Page (\d+)\]")
        page_sections: list[tuple[int, str]] = []

        parts = page_pattern.split(text)
        # parts alternates: [text_before_first_marker, page_num, page_text, page_num, page_text, ...]
        i = 1
        while i < len(parts) - 1:
            try:
                page_num = int(parts[i])
                page_text = parts[i + 1].lower()
                page_sections.append((page_num, page_text))
            except (ValueError, IndexError):
                pass
            i += 2

        if not page_sections:
            return None

        total_pages = len(page_sections)

        # Count how many pages each term appears on (document frequency)
        term_doc_freq: dict[str, int] = {}
        for t in terms:
            term_doc_freq[t] = sum(1 for _, pt in page_sections if t in pt)

        # TF-IDF-style scoring per page
        page_scores: list[tuple[int, float]] = []
        for page_num, page_text in page_sections:
            score = 0.0
            for t in terms:
                if t in page_text:
                    df = term_doc_freq[t]
                    if df > 0:
                        score += total_pages / df
            if score > 0:
                page_scores.append((page_num, score))

        if not page_scores:
            return None

        page_scores.sort(key=lambda x: x[1], reverse=True)
        best_score = page_scores[0][1]
        threshold = best_score * 0.6

        core_pages: set[int] = set()
        for page_num, score in page_scores:
            if score >= threshold:
                core_pages.add(page_num)

        max_page = max(pn for pn, _ in page_sections)
        expanded: set[int] = set()
        for p in core_pages:
            expanded.add(max(1, p - 1))
            expanded.add(p)
            expanded.add(min(max_page, p + 1))

        page_list = sorted(expanded)
        if len(page_list) > 12:
            page_list = page_list[:12]

        return page_list

    # ── ToolProvider Protocol ───────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "screens"

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="display",
                slash_command="display",
                slash_help=(
                    "Push content to a screen: /display <action> "
                    "screen_name=... (actions: show_document, show_text, "
                    "show_images, list_screens, clear)"
                ),
                description=(
                    "Push content to a named display screen. "
                    "Screens are browser tabs on monitors around the shop/office. "
                    "Screen names typically end with 'screen' (e.g., 'battery assembly screen'). "
                    "Can display documents from the knowledge store (by search query), "
                    "show specific pages from a PDF, display text/markdown content, "
                    "or show images. Use list_screens action to see connected screens. "
                    "IMPORTANT: Always execute the requested action — even if you "
                    "believe the content is already displayed. The user may need a "
                    "refresh, or the screen may have changed."
                ),
                parameters=[
                    ToolParameter(
                        name="action",
                        type=ToolParameterType.STRING,
                        description=(
                            "Action: 'show_document' (display a document by query or path), "
                            "'show_text' (display text/markdown), "
                            "'show_images' (display image gallery), "
                            "'list_screens' (list connected screens), "
                            "'clear' (clear screen back to idle)."
                        ),
                        enum=["show_document", "show_text", "show_images", "list_screens", "clear"],
                    ),
                    ToolParameter(
                        name="screen_name",
                        type=ToolParameterType.STRING,
                        description="Target screen name (e.g., 'battery assembly screen'). Required for all except list_screens.",
                        required=False,
                    ),
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Search query to find a document (e.g., 'AEM VCU pinout'). For show_document.",
                        required=False,
                    ),
                    ToolParameter(
                        name="document_path",
                        type=ToolParameterType.STRING,
                        description="Direct document path as source_id/path (e.g., 'local/docs/manual.pdf'). For show_document.",
                        required=False,
                    ),
                    ToolParameter(
                        name="pages",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "Specific page numbers to extract (1-indexed). PDF only. For show_document. "
                            "IMPORTANT: If you already know the relevant page numbers from prior "
                            "conversation or search results, pass them here directly instead of "
                            "relying on automatic page detection. This is much more accurate."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="Content title. Required for show_text.",
                        required=False,
                    ),
                    ToolParameter(
                        name="content",
                        type=ToolParameterType.STRING,
                        description="Text/markdown content to display. Required for show_text.",
                        required=False,
                    ),
                    ToolParameter(
                        name="images",
                        type=ToolParameterType.ARRAY,
                        description="List of image objects with 'url' and optional 'caption'. For show_images.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "display":
            raise KeyError(f"Unknown tool: {name}")

        action = arguments.get("action", "")
        try:
            match action:
                case "list_screens":
                    return self._tool_list_screens()
                case "clear":
                    return self._tool_clear(arguments)
                case "show_text":
                    return self._tool_show_text(arguments)
                case "show_document":
                    return await self._tool_show_document(arguments)
                case "show_images":
                    return self._tool_show_images(arguments)
                case _:
                    return f"Unknown action '{action}'."
        except Exception as e:
            logger.exception("Display tool error: action=%s", action)
            return f"Display tool error: {e}"

    def _tool_list_screens(self) -> str:
        screens = self.list_screens()
        if not screens:
            return "No screens are connected. Open /screens in a browser to set one up."
        names = [s["name"] for s in screens]
        listing = ", ".join(f'"{n}"' for n in names)
        return f"{len(screens)} screen{'s' if len(screens) != 1 else ''} connected: {listing}."

    def _tool_clear(self, arguments: dict[str, Any]) -> str:
        screen_name = arguments.get("screen_name")
        if not screen_name:
            return "I need a screen name to clear."

        resolved, error = self.resolve_screen(screen_name)
        if error:
            return error

        self.push_clear(resolved)
        return f"Cleared the '{resolved}' screen."

    def _tool_show_text(self, arguments: dict[str, Any]) -> str:
        screen_name = arguments.get("screen_name")
        if not screen_name:
            return "I need a screen name."
        content = arguments.get("content")
        if not content:
            return "I need content to display."

        resolved, error = self.resolve_screen(screen_name)
        if error:
            return error

        title = arguments.get("title", "Gilbert")
        self.push_text(resolved, title, content)
        return f'Displaying "{title}" on {resolved}.'

    async def _tool_show_document(self, arguments: dict[str, Any]) -> str:
        screen_name = arguments.get("screen_name")
        if not screen_name:
            return "I need a screen name."

        resolved, error = self.resolve_screen(screen_name)
        if error:
            return error

        query = arguments.get("query")
        document_path = arguments.get("document_path")
        pages: list[int] | None = arguments.get("pages")

        if not query and not document_path:
            return "I need a search query or document path."

        # Show loading
        search_desc = query or document_path
        self.push_loading(resolved, f"Searching for {search_desc}...")
        await asyncio.sleep(0)

        if self._knowledge is None:
            self.push_error(resolved, "Knowledge service not available.")
            return "Knowledge service is not available — can't search for documents."

        # Search for the document
        if query:
            from gilbert.interfaces.knowledge import SearchResponse

            # Vector search to find the best-matching document
            results: SearchResponse = await self._knowledge.search(query, n_results=5)
            if not results.results:
                self.push_error(resolved, f'No documents found for "{query}".')
                return f'No documents found matching "{query}".'

            top = results.results[0]
            doc_source = top.source_id
            doc_path = top.path
            doc_name = top.name or Path(doc_path).name

            # Find relevant pages by keyword-searching the document's text.
            # Try cached text first (fast, includes OCR/Vision enrichment),
            # then fall back to downloading the PDF.
            if pages is None:
                file_ext = Path(doc_path).suffix.lower()
                if file_ext == ".pdf":
                    # Try cached text (includes Vision/OCR content)
                    cached_text = await self._knowledge.get_cached_text(top.document_id)
                    if cached_text:
                        pages = self._find_pages_by_keyword_from_text(cached_text, query)
                    else:
                        # Fallback: download and extract with pypdf
                        backend = self._knowledge.get_backend(doc_source)
                        if backend is not None:
                            try:
                                doc_content = await backend.get_document(doc_path)
                                pages = self._find_pages_by_keyword(doc_content.data, query)
                            except Exception:
                                logger.warning("Failed to keyword-search document for pages", exc_info=True)
        elif document_path:
            # Direct path: source_id/path
            parts = document_path.split("/", 1)
            if len(parts) != 2:
                self.push_error(resolved, "Invalid document path format.")
                return "Document path should be source_id/path (e.g., 'local/docs/manual.pdf')."
            doc_source, doc_path = parts
            doc_name = Path(doc_path).name
        else:
            return "I need a query or document_path."

        # Build serve URL
        file_ext = Path(doc_path).suffix.lower()
        is_pdf = file_ext == ".pdf"

        # For PDFs, extract only the relevant pages (from search or explicit)
        if is_pdf and pages:
            backend = self._knowledge.get_backend(doc_source)
            if backend is None:
                self.push_error(resolved, f"Document source '{doc_source}' not found.")
                return f"Document source '{doc_source}' not available."

            try:
                doc_content = await backend.get_document(doc_path)
                token = self.extract_pages(doc_content.data, pages)
                page_desc = ", ".join(str(p) for p in sorted(pages))
                tmp_url = f"/screens/tmp/{token}"
                self.push_document(resolved, f"{doc_name} (p. {page_desc})", tmp_url, "pdf", tmp_token=token)
                return f'Displaying page{"s" if len(pages) != 1 else ""} {page_desc} of "{doc_name}" on {resolved}.'
            except ValueError as e:
                self.push_error(resolved, str(e))
                return str(e)
            except Exception as e:
                logger.exception("Failed to extract pages")
                self.push_error(resolved, f"Failed to extract pages: {e}")
                return f"Failed to extract pages: {e}"

        # Non-PDF or no page info — serve the whole document
        serve_url = f"/documents/serve/{doc_source}/{doc_path}"
        content_type = "pdf" if is_pdf else ("image" if file_ext in _IMAGE_EXTS else "other")
        self.push_document(resolved, doc_name, serve_url, content_type)
        return f'Displaying "{doc_name}" on {resolved}.'

    def _tool_show_images(self, arguments: dict[str, Any]) -> str:
        screen_name = arguments.get("screen_name")
        if not screen_name:
            return "I need a screen name."

        resolved, error = self.resolve_screen(screen_name)
        if error:
            return error

        images = arguments.get("images", [])
        if not images:
            return "I need a list of images to display."

        title = arguments.get("title", "Images")
        self.push_images(resolved, title, images)
        return f'Showing {len(images)} image{"s" if len(images) != 1 else ""} on {resolved}.'

    # --- WebSocket RPC handlers ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "screens.list": self._ws_screens_list,
        }

    async def _ws_screens_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        screens = self.list_screens()
        return {"type": "screens.list.result", "ref": frame.get("id"), "screens": screens}
