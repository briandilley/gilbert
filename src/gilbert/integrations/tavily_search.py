"""Tavily Search backend — web search via the Tavily API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from gilbert.interfaces.websearch import WebSearchBackend, WebSearchResult

logger = logging.getLogger(__name__)

_API_URL = "https://api.tavily.com/search"
_DEFAULT_TIMEOUT = 15


class TavilySearch(WebSearchBackend):
    """Tavily Search API implementation."""

    backend_name = "tavily"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="api_key", type=ToolParameterType.STRING,
                description="Tavily API key.",
                sensitive=True, restart_required=True,
            ),
            ConfigParam(
                key="timeout", type=ToolParameterType.INTEGER,
                description="Request timeout in seconds.",
                default=_DEFAULT_TIMEOUT,
            ),
        ]

    def __init__(self) -> None:
        self._api_key: str = ""
        self._client: httpx.AsyncClient | None = None
        self._timeout: int = _DEFAULT_TIMEOUT

    async def initialize(self, config: dict[str, Any]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._timeout = int(config.get("timeout", _DEFAULT_TIMEOUT))
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self, query: str, count: int = 5,
    ) -> list[WebSearchResult]:
        if self._client is None:
            raise RuntimeError("TavilySearch not initialized")

        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": count,
            "search_depth": "basic",
            "include_answer": True,
        }

        response = await self._client.post(_API_URL, json=payload)
        response.raise_for_status()
        data = response.json()

        results: list[WebSearchResult] = []

        # Include Tavily's AI-generated answer as the first result if present
        answer = data.get("answer")
        if answer:
            results.append(WebSearchResult(
                title="AI Summary",
                url="",
                snippet=answer,
            ))

        for item in data.get("results", []):
            content = item.get("content", "")
            if len(content) > 500:
                content = content[:500] + "..."
            results.append(WebSearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=content,
            ))

        return results
