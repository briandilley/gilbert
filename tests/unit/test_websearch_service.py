"""Tests for WebSearchService and WebSearchBackend."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.core.services.websearch import WebSearchService
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.websearch import WebSearchBackend, WebSearchResult


# --- Stubs ---


class StubSearchBackend(WebSearchBackend):
    """In-memory search backend for testing."""

    def __init__(self) -> None:
        self.initialized = False
        self.init_config: dict[str, Any] = {}
        self._results: list[WebSearchResult] = []
        self._image_urls: list[str] = []

    def set_results(self, results: list[WebSearchResult]) -> None:
        self._results = results

    def set_image_urls(self, urls: list[str]) -> None:
        self._image_urls = urls

    async def initialize(self, config: dict[str, Any]) -> None:
        self.init_config = config
        self.initialized = True

    async def close(self) -> None:
        self.initialized = False

    async def search(self, query: str, count: int = 5) -> list[WebSearchResult]:
        return self._results[:count]

    async def search_images(self, query: str, count: int = 5) -> list[str]:
        return self._image_urls[:count]


class ErrorSearchBackend(WebSearchBackend):
    """Backend that always raises on search."""

    async def initialize(self, config: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass

    async def search(self, query: str, count: int = 5) -> list[WebSearchResult]:
        raise RuntimeError("Search API unavailable")


# --- Fixtures ---


@pytest.fixture
def stub_backend() -> StubSearchBackend:
    return StubSearchBackend()


@pytest.fixture
def service(stub_backend: StubSearchBackend) -> WebSearchService:
    svc = WebSearchService()
    svc._backend = stub_backend
    svc._enabled = True
    svc._settings = {"api_key": "test-key-123"}
    return svc


@pytest.fixture
def resolver() -> MagicMock:
    r = MagicMock(spec=ServiceResolver)
    r.require_capability.side_effect = LookupError("not available")
    r.get_capability.return_value = None
    return r


@pytest.fixture
async def started_service(
    service: WebSearchService, stub_backend: StubSearchBackend,
) -> WebSearchService:
    import httpx
    await stub_backend.initialize(service._settings)
    service._http_client = httpx.AsyncClient(timeout=30, follow_redirects=True)
    return service


# --- Service Lifecycle ---


class TestServiceLifecycle:
    def test_service_info(self, service: WebSearchService) -> None:
        info = service.service_info()
        assert info.name == "websearch"
        assert "websearch" in info.capabilities
        assert "ai_tools" in info.capabilities

    @pytest.mark.asyncio
    async def test_start_initializes_backend(
        self, started_service: WebSearchService,
        stub_backend: StubSearchBackend,
    ) -> None:
        assert stub_backend.initialized
        assert stub_backend.init_config["api_key"] == "test-key-123"

    @pytest.mark.asyncio
    async def test_stop_closes_backend(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        await started_service.stop()
        assert not stub_backend.initialized



# --- ToolProvider ---


class TestToolProvider:
    def test_tool_provider_name(self, service: WebSearchService) -> None:
        assert service.tool_provider_name == "websearch"

    def test_get_tools(self, service: WebSearchService) -> None:
        tools = service.get_tools()
        names = {t.name for t in tools}
        assert names == {"web_search", "image_search", "fetch_url", "fetch_url_raw"}


# --- Tool Execution ---


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_search_returns_results(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_results([
            WebSearchResult(title="Result 1", url="https://example.com/1", snippet="First result"),
            WebSearchResult(title="Result 2", url="https://example.com/2", snippet="Second result"),
        ])
        result = await started_service.execute_tool(
            "web_search", {"query": "test query"},
        )
        assert "Result 1" in result
        assert "https://example.com/1" in result
        assert "Result 2" in result

    @pytest.mark.asyncio
    async def test_search_with_ai_summary(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_results([
            WebSearchResult(title="AI Summary", url="", snippet="The answer is 42"),
            WebSearchResult(title="Source", url="https://example.com", snippet="Details"),
        ])
        result = await started_service.execute_tool(
            "web_search", {"query": "meaning of life"},
        )
        assert "AI Summary" in result
        assert "The answer is 42" in result

    @pytest.mark.asyncio
    async def test_search_no_results(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_results([])
        result = await started_service.execute_tool(
            "web_search", {"query": "obscure query"},
        )
        assert "No results" in result

    @pytest.mark.asyncio
    async def test_search_empty_query(
        self, started_service: WebSearchService,
    ) -> None:
        result = await started_service.execute_tool(
            "web_search", {"query": ""},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_search_respects_count(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_results([
            WebSearchResult(title=f"R{i}", url=f"https://example.com/{i}", snippet=f"Result {i}")
            for i in range(10)
        ])
        result = await started_service.execute_tool(
            "web_search", {"query": "test", "count": 2},
        )
        assert "R0" in result
        assert "R1" in result
        assert "R2" not in result

    @pytest.mark.asyncio
    async def test_count_capped_at_10(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_results([
            WebSearchResult(title=f"R{i}", url=f"https://example.com/{i}", snippet=f"Result {i}")
            for i in range(20)
        ])
        result = await started_service.execute_tool(
            "web_search", {"query": "test", "count": 50},
        )
        # Should cap at 10
        assert "R9" in result
        assert "R10" not in result

    @pytest.mark.asyncio
    async def test_search_error_handled(self) -> None:
        svc = WebSearchService()
        svc._backend = ErrorSearchBackend()
        svc._enabled = True
        result = await svc.execute_tool(
            "web_search", {"query": "test"},
        )
        assert "error" in result.lower()
        assert "Search failed" in result

    @pytest.mark.asyncio
    async def test_unknown_tool(
        self, started_service: WebSearchService,
    ) -> None:
        with pytest.raises(KeyError, match="Unknown tool"):
            await started_service.execute_tool("nonexistent", {})


# --- Image Search ---


class TestImageSearch:
    @pytest.mark.asyncio
    async def test_image_search_returns_markdown(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_image_urls([
            "https://example.com/img1.jpg",
            "https://example.com/img2.jpg",
        ])
        result = await started_service.execute_tool(
            "image_search", {"query": "sunset"},
        )
        assert "![sunset - image 1](https://example.com/img1.jpg)" in result
        assert "![sunset - image 2](https://example.com/img2.jpg)" in result
        assert "Found 2 image(s)" in result

    @pytest.mark.asyncio
    async def test_image_search_no_results(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_image_urls([])
        result = await started_service.execute_tool(
            "image_search", {"query": "nonexistent"},
        )
        data = json.loads(result)
        assert data["images"] == []
        assert "No images found" in data["message"]

    @pytest.mark.asyncio
    async def test_image_search_empty_query(
        self, started_service: WebSearchService,
    ) -> None:
        result = await started_service.execute_tool(
            "image_search", {"query": ""},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_image_search_count_capped_at_5(
        self, started_service: WebSearchService, stub_backend: StubSearchBackend,
    ) -> None:
        stub_backend.set_image_urls([f"https://example.com/{i}.jpg" for i in range(10)])
        result = await started_service.execute_tool(
            "image_search", {"query": "test", "count": 20},
        )
        # Should have at most 5 images
        assert "image 5" in result
        assert "image 6" not in result


# --- Fetch URL ---


class TestFetchUrl:
    @pytest.mark.asyncio
    async def test_fetch_empty_url(
        self, started_service: WebSearchService,
    ) -> None:
        result = await started_service.execute_tool("fetch_url", {"url": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_fetch_html_page(
        self, started_service: WebSearchService,
    ) -> None:
        html = (
            "<html><head><title>Test</title></head>"
            "<body><h1>Hello</h1><p>World <a href='https://example.com'>link</a></p>"
            "<script>var x = 1;</script></body></html>"
        )

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = html
            resp.raise_for_status = MagicMock()
            return resp

        started_service._http_client = AsyncMock()
        started_service._http_client.get = mock_get

        result = await started_service.execute_tool(
            "fetch_url", {"url": "https://example.com"},
        )
        assert "Hello" in result
        assert "World" in result
        # Script content should be stripped
        assert "var x" not in result

    @pytest.mark.asyncio
    async def test_fetch_with_links(
        self, started_service: WebSearchService,
    ) -> None:
        html = (
            "<html><body>"
            "<a href='https://a.com'>Link A</a>"
            "<a href='https://b.com'>Link B</a>"
            "</body></html>"
        )

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = html
            resp.raise_for_status = MagicMock()
            return resp

        started_service._http_client = AsyncMock()
        started_service._http_client.get = mock_get

        result = await started_service.execute_tool(
            "fetch_url", {"url": "https://example.com", "include_links": True},
        )
        assert "Link A" in result
        assert "https://a.com" in result
        assert "Links found (2)" in result

    @pytest.mark.asyncio
    async def test_fetch_plain_text(
        self, started_service: WebSearchService,
    ) -> None:
        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/plain"}
            resp.text = "Just plain text content"
            resp.raise_for_status = MagicMock()
            return resp

        started_service._http_client = AsyncMock()
        started_service._http_client.get = mock_get

        result = await started_service.execute_tool(
            "fetch_url", {"url": "https://example.com/file.txt"},
        )
        assert "Just plain text content" in result

    @pytest.mark.asyncio
    async def test_fetch_unsupported_content_type(
        self, started_service: WebSearchService,
    ) -> None:
        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "application/pdf"}
            resp.text = ""
            resp.raise_for_status = MagicMock()
            return resp

        started_service._http_client = AsyncMock()
        started_service._http_client.get = mock_get

        result = await started_service.execute_tool(
            "fetch_url", {"url": "https://example.com/file.pdf"},
        )
        assert "Unsupported content type" in result

    @pytest.mark.asyncio
    async def test_fetch_prepends_https(
        self, started_service: WebSearchService,
    ) -> None:
        """URLs without scheme should get https:// prepended."""
        call_urls: list[str] = []

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            call_urls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = "<html><body>OK</body></html>"
            resp.raise_for_status = MagicMock()
            return resp

        started_service._http_client = AsyncMock()
        started_service._http_client.get = mock_get

        await started_service.execute_tool(
            "fetch_url", {"url": "example.com"},
        )
        assert call_urls[0] == "https://example.com"


# --- Fetch URL Raw ---


class TestFetchUrlRaw:
    @pytest.mark.asyncio
    async def test_fetch_raw_returns_unprocessed(
        self, started_service: WebSearchService,
    ) -> None:
        html = "<html><head><title>Test</title></head><body><script>var x=1;</script><p>Hello</p></body></html>"

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.text = html
            resp.raise_for_status = MagicMock()
            return resp

        started_service._http_client = AsyncMock()
        started_service._http_client.get = mock_get

        result = await started_service.execute_tool(
            "fetch_url_raw", {"url": "https://example.com"},
        )
        # Raw should include script tags and HTML markup — not stripped
        assert "<script>" in result
        assert "<p>Hello</p>" in result

    @pytest.mark.asyncio
    async def test_fetch_raw_empty_url(
        self, started_service: WebSearchService,
    ) -> None:
        result = await started_service.execute_tool("fetch_url_raw", {"url": ""})
        assert "error" in result
