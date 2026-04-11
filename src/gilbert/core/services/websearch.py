"""Web search service — exposes web search and URL fetching as AI tools."""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Any

import httpx


from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
)
from gilbert.interfaces.websearch import WebSearchBackend

logger = logging.getLogger(__name__)


_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "svg", "path", "head", "meta", "link",
})
_MAX_FETCH_SIZE = 500_000  # 500KB
_FETCH_TIMEOUT = 15


class _HTMLToText(HTMLParser):
    """Lightweight HTML → text converter using only stdlib."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self._links: list[tuple[str, str]] = []
        self._current_href: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href and href.startswith("http"):
                self._current_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._current_href:
            self._current_href = ""

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)
            if self._current_href:
                self._links.append((text, self._current_href))

    def get_text(self) -> str:
        raw = " ".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r" +", " ", raw)).strip()

    def get_links(self) -> list[tuple[str, str]]:
        return self._links


class WebSearchService(Service, ToolProvider):
    """Wraps a WebSearchBackend as a discoverable service with AI tools."""

    def __init__(self) -> None:
        self._backend: WebSearchBackend | None = None
        self._backend_name: str = "tavily"
        self._enabled: bool = False
        self._settings: dict[str, Any] = {}
        self._http_client: httpx.AsyncClient | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="websearch",
            capabilities=frozenset({"websearch", "ai_tools"}),
            toggleable=True,
            toggle_description="Web search and URL fetching",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        # Load config
        backend_name = "tavily"
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("websearch")
                self._settings = section.get("settings", self._settings)
                backend_name = section.get("backend", "tavily")

                if not section.get("enabled", False):
                    logger.info("Web search service disabled")
                    return

        self._enabled = True
        self._backend_name = backend_name

        try:
            import gilbert.integrations.tavily_search  # noqa: F401
        except ImportError:
            pass

        backends = WebSearchBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown web search backend: {backend_name}")
        self._backend = backend_cls()

        # Initialize backend with settings (includes API key)
        await self._backend.initialize(self._settings)
        self._http_client = httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Gilbert/1.0"},
        )
        logger.info("Web search service started")

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._enabled = False

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "websearch"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam

        # Import known backends so they register before we query the registry
        try:
            import gilbert.integrations.tavily_search  # noqa: F401
        except ImportError:
            pass

        params = [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Web search backend provider.",
                default="tavily", restart_required=True,
                choices=tuple(WebSearchBackend.registered_backends().keys()) or ("tavily",),
            ),
        ]
        backends = WebSearchBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(ConfigParam(
                    key=f"settings.{bp.key}", type=bp.type,
                    description=bp.description, default=bp.default,
                    restart_required=bp.restart_required, sensitive=bp.sensitive,
                    choices=bp.choices, choices_from=bp.choices_from,
                    multiline=bp.multiline, backend_param=True,
                ))
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._settings = config.get("settings", self._settings)

    # ── ToolProvider ─────────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "websearch"

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="web_search",
                description=(
                    "Search the web for current information. "
                    "Use when the user asks about recent events, facts you're "
                    "unsure about, or anything that may require up-to-date information."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="The search query.",
                    ),
                    ToolParameter(
                        name="count",
                        type=ToolParameterType.INTEGER,
                        description="Number of results to return (default 5, max 10).",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="fetch_url",
                description=(
                    "Fetch the content of a web page and extract its text. "
                    "Use when the user provides a URL and wants to analyze, "
                    "summarize, or extract information from that page."
                ),
                parameters=[
                    ToolParameter(
                        name="url",
                        type=ToolParameterType.STRING,
                        description="The URL to fetch.",
                    ),
                    ToolParameter(
                        name="include_links",
                        type=ToolParameterType.BOOLEAN,
                        description="Include a list of links found on the page (default false).",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="fetch_url_raw",
                description=(
                    "Fetch the raw HTML or text content of a URL without processing. "
                    "Use when the user needs the original source code, raw markup, "
                    "or unprocessed content of a page."
                ),
                parameters=[
                    ToolParameter(
                        name="url",
                        type=ToolParameterType.STRING,
                        description="The URL to fetch.",
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "web_search":
                return await self._exec_web_search(arguments)
            case "fetch_url":
                return await self._exec_fetch_url(arguments)
            case "fetch_url_raw":
                return await self._exec_fetch_url_raw(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _exec_fetch_url(self, arguments: dict[str, Any]) -> str:
        url = arguments.get("url", "")
        if not url:
            return json.dumps({"error": "url is required"})

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        include_links = bool(arguments.get("include_links", False))

        if self._http_client is None:
            return json.dumps({"error": "Service not initialized"})

        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
        except httpx.TimeoutException:
            return json.dumps({"error": f"Timed out fetching {url}"})
        except httpx.HTTPStatusError as exc:
            return json.dumps({"error": f"HTTP {exc.response.status_code} fetching {url}"})
        except Exception as exc:
            return json.dumps({"error": f"Failed to fetch {url}: {exc}"})

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return json.dumps({
                "url": url,
                "error": f"Unsupported content type: {content_type}",
            })

        body = response.text[:_MAX_FETCH_SIZE]

        if "text/plain" in content_type:
            if len(body) > 50_000:
                body = body[:50_000] + "\n\n[... truncated]"
            return f"Content of {url}:\n\n{body}"

        # Parse HTML to text
        parser = _HTMLToText()
        parser.feed(body)
        text = parser.get_text()

        if len(text) > 50_000:
            text = text[:50_000] + "\n\n[... truncated]"

        parts = [f"Content of {url}:\n\n{text}"]

        if include_links:
            links = parser.get_links()
            if links:
                parts.append(f"\n\nLinks found ({len(links)}):")
                for label, href in links[:100]:
                    parts.append(f"  - [{label}]({href})")
                if len(links) > 100:
                    parts.append(f"  ... and {len(links) - 100} more")

        return "\n".join(parts)

    async def _exec_fetch_url_raw(self, arguments: dict[str, Any]) -> str:
        url = arguments.get("url", "")
        if not url:
            return json.dumps({"error": "url is required"})

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        if self._http_client is None:
            return json.dumps({"error": "Service not initialized"})

        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
        except httpx.TimeoutException:
            return json.dumps({"error": f"Timed out fetching {url}"})
        except httpx.HTTPStatusError as exc:
            return json.dumps({"error": f"HTTP {exc.response.status_code} fetching {url}"})
        except Exception as exc:
            return json.dumps({"error": f"Failed to fetch {url}: {exc}"})

        body = response.text[:_MAX_FETCH_SIZE]
        if len(body) > 50_000:
            body = body[:50_000] + "\n\n[... truncated at 50,000 characters]"
        return body

    async def _exec_web_search(self, arguments: dict[str, Any]) -> str:

        query = arguments.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})

        if self._backend is None:
            return json.dumps({"error": "Service not initialized"})

        count = min(int(arguments.get("count", 5)), 10)

        try:
            results = await self._backend.search(query, count=count)
        except Exception as exc:
            logger.warning("Web search failed for %r: %s", query, exc)
            return json.dumps({"error": f"Search failed: {exc}"})

        if not results:
            return json.dumps({"query": query, "results": [], "message": "No results found"})

        # Format as readable text for the AI
        parts = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            if r.url:
                parts.append(f"{i}. **{r.title}**\n   {r.url}\n   {r.snippet}\n")
            else:
                # AI summary (no URL)
                parts.append(f"**{r.title}:** {r.snippet}\n")

        return "\n".join(parts)
