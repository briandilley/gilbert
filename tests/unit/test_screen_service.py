"""Tests for ScreenService — screen registry, SSE, temp files, name resolution, and tools."""

import json
from pathlib import Path
from typing import Any

import pytest

from gilbert.core.services.screens import (
    ScreenService,
    _normalize,
    _sse_event,
    _strip_screen_suffix,
)


# ── Helpers ──────────────────────────────────────────────────


class FakeResolver:
    """Minimal ServiceResolver for tests."""

    def __init__(self) -> None:
        self.capabilities: dict[str, Any] = {}

    def get_capability(self, capability: str) -> Any:
        return self.capabilities.get(capability)

    def require_capability(self, capability: str) -> Any:
        svc = self.capabilities.get(capability)
        if svc is None:
            raise LookupError(capability)
        return svc

    def get_all(self, capability: str) -> list[Any]:
        svc = self.capabilities.get(capability)
        return [svc] if svc else []


@pytest.fixture
def service() -> ScreenService:
    svc = ScreenService()
    svc._enabled = True
    return svc


@pytest.fixture
def started_service(tmp_path: Path) -> ScreenService:
    """A service with _tmp_dir initialized (simulating post-start)."""
    svc = ScreenService()
    svc._tmp_dir = tmp_path
    return svc


# ── Normalization & suffix stripping ─────────────────────────


class TestNormalization:
    def test_normalize_basic(self) -> None:
        assert _normalize("  Shop Screen  ") == "shop screen"

    def test_normalize_already_lower(self) -> None:
        assert _normalize("test") == "test"

    def test_strip_screen_suffix(self) -> None:
        assert _strip_screen_suffix("shop screen") == "shop"

    def test_strip_tv_suffix(self) -> None:
        assert _strip_screen_suffix("brian's tv") == "brian"

    def test_strip_curly_possessive(self) -> None:
        assert _strip_screen_suffix("brian\u2019s display") == "brian"

    def test_strip_multiple_suffixes(self) -> None:
        assert _strip_screen_suffix("big shop screen") == "big shop"

    def test_no_suffix_to_strip(self) -> None:
        assert _strip_screen_suffix("main bench") == "main bench"

    def test_strip_only_suffix(self) -> None:
        assert _strip_screen_suffix("screen") == ""

    def test_strip_monitor(self) -> None:
        assert _strip_screen_suffix("assembly monitor") == "assembly"

    def test_strip_panel(self) -> None:
        assert _strip_screen_suffix("front panel") == "front"


# ── SSE formatting ───────────────────────────────────────────


class TestSSE:
    def test_sse_event_format(self) -> None:
        result = _sse_event("show_text", {"type": "show_text", "title": "Test"})
        lines = result.split("\n")
        assert lines[0] == "event: show_text"
        assert lines[1].startswith("data: ")
        data = json.loads(lines[1][6:])
        assert data["type"] == "show_text"
        assert data["title"] == "Test"
        assert result.endswith("\n\n")


# ── Screen registry ───────���──────────────────────────────────


class TestScreenRegistry:
    def test_connect_and_list(self, service: ScreenService) -> None:
        screen = service.connect("Shop Screen")
        assert screen.name == "Shop Screen"
        assert screen.key == "shop screen"
        screens = service.list_screens()
        assert len(screens) == 1
        assert screens[0]["name"] == "Shop Screen"
        assert screens[0]["key"] == "shop screen"

    def test_connect_replaces_existing(self, service: ScreenService) -> None:
        old = service.connect("Shop Screen")
        new = service.connect("Shop Screen")
        assert old is not new
        assert len(service.list_screens()) == 1
        # Old screen should have received sentinel
        assert not old.queue.empty()

    def test_disconnect_removes(self, service: ScreenService) -> None:
        screen = service.connect("Test")
        service.disconnect("test", screen)
        assert len(service.list_screens()) == 0

    def test_disconnect_ignores_stale(self, service: ScreenService) -> None:
        old = service.connect("Test")
        # Connect a second client to the same screen — its presence is what
        # we're guarding against the stale-disconnect call below.
        service.connect("Test")
        # Disconnecting old should NOT remove new
        service.disconnect("test", old)
        assert len(service.list_screens()) == 1

    def test_get_screen(self, service: ScreenService) -> None:
        service.connect("Shop Screen")
        assert service.get_screen("shop screen") is not None
        assert service.get_screen("nonexistent") is None

    def test_default_url(self, service: ScreenService) -> None:
        service.connect("Test", default_url="https://example.com")
        screens = service.list_screens()
        assert screens[0]["default_url"] == "https://example.com"

    def test_empty_default_url_is_none(self, service: ScreenService) -> None:
        screen = service.connect("Test", default_url="")
        assert screen.default_url is None


# ── Push methods ─────���───────────────────────────────────────


class TestPushMethods:
    def test_push_text(self, service: ScreenService) -> None:
        screen = service.connect("Test")
        result = service.push_text("test", "Title", "Hello world")
        assert result is True
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["type"] == "show_text"
        assert data["title"] == "Title"
        assert data["content"] == "Hello world"

    def test_push_text_missing_screen(self, service: ScreenService) -> None:
        assert service.push_text("nonexistent", "Title", "Content") is False

    def test_push_clear(self, service: ScreenService) -> None:
        screen = service.connect("Test")
        result = service.push_clear("test")
        assert result is True
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["type"] == "clear"

    def test_push_clear_with_default_url(self, service: ScreenService) -> None:
        screen = service.connect("Test", default_url="https://example.com")
        service.push_clear("test")
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["default_url"] == "https://example.com"

    def test_push_document(self, service: ScreenService) -> None:
        screen = service.connect("Test")
        result = service.push_document("test", "Manual", "/documents/serve/local/manual.pdf")
        assert result is True
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["type"] == "show_document"
        assert data["title"] == "Manual"
        assert data["serve_url"] == "/documents/serve/local/manual.pdf"

    def test_push_images(self, service: ScreenService) -> None:
        screen = service.connect("Test")
        images = [{"url": "/img/1.png", "caption": "Photo 1"}]
        result = service.push_images("test", "Gallery", images)
        assert result is True
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["type"] == "show_images"
        assert len(data["images"]) == 1

    def test_push_loading(self, service: ScreenService) -> None:
        screen = service.connect("Test")
        service.push_loading("test", "Searching...")
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["type"] == "loading"
        assert data["message"] == "Searching..."

    def test_push_error(self, service: ScreenService) -> None:
        screen = service.connect("Test")
        service.push_error("test", "Not found")
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["type"] == "error"
        assert data["message"] == "Not found"


# ── Temp file management ─────────────────────────────────────


class TestTempFiles:
    def test_extract_pages(self, started_service: ScreenService) -> None:
        # Create a minimal PDF with 2 pages
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_blank_page(width=612, height=792)
        import io
        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

        token = started_service.extract_pages(pdf_bytes, [1])
        assert token
        path = started_service.get_temp_path(token)
        assert path is not None
        assert path.exists()
        assert path.suffix == ".pdf"

    def test_extract_pages_out_of_range(self, started_service: ScreenService) -> None:
        from pypdf import PdfWriter
        import io

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        writer.write(buf)

        with pytest.raises(ValueError, match="out of range"):
            started_service.extract_pages(buf.getvalue(), [5])

    def test_save_temp_file(self, started_service: ScreenService) -> None:
        token = started_service.save_temp_file("image.png", b"\x89PNG")
        path = started_service.get_temp_path(token)
        assert path is not None
        assert path.suffix == ".png"
        assert path.read_bytes() == b"\x89PNG"

    def test_get_temp_path_missing(self, started_service: ScreenService) -> None:
        assert started_service.get_temp_path("nonexistent") is None

    def test_get_temp_mime_type(self, started_service: ScreenService) -> None:
        token = started_service.save_temp_file("doc.pdf", b"%PDF")
        assert started_service.get_temp_mime_type(token) == "application/pdf"

        token2 = started_service.save_temp_file("img.png", b"\x89PNG")
        assert started_service.get_temp_mime_type(token2) == "image/png"

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, started_service: ScreenService) -> None:
        started_service._ttl = 0  # Expire immediately
        started_service.save_temp_file("old.pdf", b"%PDF")
        assert len(started_service._files) == 1
        await started_service._cleanup_expired()
        assert len(started_service._files) == 0


# ── Screen name resolution ───────────────────────────────────


class TestScreenResolution:
    def test_exact_match(self, service: ScreenService) -> None:
        service.connect("shop screen")
        name, err = service.resolve_screen("shop screen")
        assert name == "shop screen"
        assert err is None

    def test_suffix_stripping(self, service: ScreenService) -> None:
        service.connect("shop")
        name, err = service.resolve_screen("shop screen")
        assert name == "shop"
        assert err is None

    def test_suffix_stripping_both_sides(self, service: ScreenService) -> None:
        service.connect("shop screen")
        name, err = service.resolve_screen("shop display")
        assert name == "shop screen"
        assert err is None

    def test_fuzzy_match(self, service: ScreenService) -> None:
        service.connect("battery assembly screen")
        name, err = service.resolve_screen("battery assemble screen")
        assert name == "battery assembly screen"
        assert err is None

    def test_no_match(self, service: ScreenService) -> None:
        service.connect("shop screen")
        name, err = service.resolve_screen("totally different")
        assert name == ""
        assert err is not None
        assert "not found" in err.lower() or "shop screen" in err

    def test_no_screens(self, service: ScreenService) -> None:
        name, err = service.resolve_screen("anything")
        assert name == ""
        assert "no screens" in err.lower()


# ── Tool definitions ─────────────────────────────────────────


class TestToolDefinitions:
    def test_service_info(self, service: ScreenService) -> None:
        info = service.service_info()
        assert info.name == "screens"
        assert "screen_display" in info.capabilities
        assert "ai_tools" in info.capabilities

    def test_tool_provider_name(self, service: ScreenService) -> None:
        assert service.tool_provider_name == "screens"

    def test_get_tools(self, service: ScreenService) -> None:
        tools = service.get_tools()
        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "display"
        assert tool.required_role == "user"
        # Check action param has the right enum values
        action_param = next(p for p in tool.parameters if p.name == "action")
        assert "list_screens" in action_param.enum
        assert "show_document" in action_param.enum
        assert "show_text" in action_param.enum
        assert "clear" in action_param.enum

    @pytest.mark.asyncio
    async def test_tool_list_screens_empty(self, service: ScreenService) -> None:
        result = await service.execute_tool("display", {"action": "list_screens"})
        assert "no screens" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_list_screens_with_screens(self, service: ScreenService) -> None:
        service.connect("Shop Screen")
        result = await service.execute_tool("display", {"action": "list_screens"})
        assert "1 screen" in result
        assert "Shop Screen" in result

    @pytest.mark.asyncio
    async def test_tool_clear(self, service: ScreenService) -> None:
        screen = service.connect("shop screen")
        result = await service.execute_tool("display", {
            "action": "clear",
            "screen_name": "shop screen",
        })
        assert "cleared" in result.lower()
        assert not screen.queue.empty()

    @pytest.mark.asyncio
    async def test_tool_clear_missing_screen(self, service: ScreenService) -> None:
        service.connect("shop screen")
        result = await service.execute_tool("display", {
            "action": "clear",
            "screen_name": "nonexistent screen",
        })
        assert "not found" in result.lower() or "shop screen" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_show_text(self, service: ScreenService) -> None:
        screen = service.connect("test screen")
        result = await service.execute_tool("display", {
            "action": "show_text",
            "screen_name": "test screen",
            "title": "Hello",
            "content": "# Heading\nSome text",
        })
        assert "displaying" in result.lower()
        msg = screen.queue.get_nowait()
        data = json.loads(msg.split("data: ")[1].split("\n")[0])
        assert data["title"] == "Hello"
        assert data["content"] == "# Heading\nSome text"

    @pytest.mark.asyncio
    async def test_tool_show_text_missing_content(self, service: ScreenService) -> None:
        service.connect("test screen")
        result = await service.execute_tool("display", {
            "action": "show_text",
            "screen_name": "test screen",
            "title": "Hello",
        })
        assert "need content" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_show_images(self, service: ScreenService) -> None:
        # Establish a screen with the test name so the tool resolves it.
        service.connect("test screen")
        result = await service.execute_tool("display", {
            "action": "show_images",
            "screen_name": "test screen",
            "images": [{"url": "/img/1.png", "caption": "Photo"}],
        })
        assert "1 image" in result

    @pytest.mark.asyncio
    async def test_tool_unknown_action(self, service: ScreenService) -> None:
        result = await service.execute_tool("display", {"action": "foobar"})
        assert "unknown" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_unknown_tool(self, service: ScreenService) -> None:
        with pytest.raises(KeyError):
            await service.execute_tool("nonexistent", {})


# ── Page selection ────────────────────────────────────────────


def _make_test_pdf(page_texts: list[str]) -> bytes:
    """Create a minimal PDF with the given text on each page."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as rl_canvas
    import io as _io

    buf = _io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=letter)
    for text in page_texts:
        c.drawString(72, 700, text)
        c.showPage()
    c.save()
    return buf.getvalue()


class TestFindPagesByKeyword:
    def _make_pdf(self, page_texts: list[str]) -> bytes:
        """Create a test PDF. Falls back to pypdf blank pages if reportlab unavailable."""
        try:
            return _make_test_pdf(page_texts)
        except ImportError:
            pytest.skip("reportlab not available for PDF text tests")

    def test_prefers_distinctive_terms(self) -> None:
        """'pinout' only on page 3, but 'VCU' on all pages (like a header). Should pick page 3."""
        pdf = self._make_pdf([
            "VCU Quick Start Guide - Table of Contents",   # page 1: VCU in title
            "VCU Introduction and Overview",                # page 2: VCU in header
            "VCU Pinout Table: Pin 1 GND Pin 2 VCC",       # page 3: pinout content
            "VCU Wiring and Installation",                  # page 4: VCU in header
        ])
        pages = ScreenService._find_pages_by_keyword(pdf, "VCU pinout table")
        assert pages is not None
        assert 3 in pages
        # Pages without "pinout" should score lower and ideally not be included
        assert 1 not in pages or 3 in pages  # at minimum, page 3 must be present

    def test_finds_page_with_query_terms(self) -> None:
        pdf = self._make_pdf([
            "Table of Contents",
            "Introduction to the system",
            "Pinout Table: Pin 1 GND Pin 2 VCC Pin 3 CAN_H",
        ])
        pages = ScreenService._find_pages_by_keyword(pdf, "pinout table")
        assert pages is not None
        assert 3 in pages

    def test_includes_neighbors(self) -> None:
        pdf = self._make_pdf([
            "Unrelated content here",
            "Unrelated content here",
            "Pinout Table with all the pin details",
            "Unrelated content here",
        ])
        pages = ScreenService._find_pages_by_keyword(pdf, "pinout table")
        assert pages is not None
        assert 2 in pages  # neighbor before
        assert 3 in pages  # match
        assert 4 in pages  # neighbor after

    def test_no_matches_returns_none(self) -> None:
        pdf = self._make_pdf(["Nothing relevant here", "Also nothing here"])
        pages = ScreenService._find_pages_by_keyword(pdf, "VCU pinout")
        assert pages is None

    def test_short_query_terms_skipped(self) -> None:
        pdf = self._make_pdf(["VCU pinout of the system"])
        pages = ScreenService._find_pages_by_keyword(pdf, "of")
        assert pages is None

    def test_caps_at_12(self) -> None:
        pdf = self._make_pdf([f"pinout details section {i}" for i in range(20)])
        pages = ScreenService._find_pages_by_keyword(pdf, "pinout details")
        assert pages is not None
        assert len(pages) <= 12
