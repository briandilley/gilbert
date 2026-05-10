"""Tests for ``RssAtomFeedBackend`` against fixture HTTP responses.

We never mock ``feedparser`` itself — that would defeat the test's
purpose. Instead, ``initialize`` accepts an injectable
``httpx.AsyncClient(transport=httpx.MockTransport(...))`` so the real
feedparser sees real fixture bytes against canned URL responses.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from gilbert.integrations.rss_feeds import RssAtomFeedBackend
from gilbert.interfaces.feeds import FeedAuthError, FeedError, FeedTooLargeError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "feeds"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _make_transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


@pytest.fixture
async def backend_atom() -> RssAtomFeedBackend:
    body = _load("atom_basic.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/atom+xml",
                "etag": '"abc-123"',
                "last-modified": "Fri, 01 May 2026 12:00:00 GMT",
            },
        )

    backend = RssAtomFeedBackend()
    await backend.initialize(
        {"user_agent": "TestUA/1.0"},
        http_client=httpx.AsyncClient(transport=_make_transport(handler)),
    )
    return backend


@pytest.fixture
async def backend_rss() -> RssAtomFeedBackend:
    body = _load("rss_basic.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})

    backend = RssAtomFeedBackend()
    await backend.initialize(
        {},
        http_client=httpx.AsyncClient(transport=_make_transport(handler)),
    )
    return backend


async def test_probe_returns_meta_for_atom(backend_atom: RssAtomFeedBackend) -> None:
    meta = await backend_atom.probe("https://example.com/feed.atom")
    assert meta.title == "Example Atom Feed"
    assert meta.link == "https://example.com/"


async def test_poll_returns_items_in_published_order(
    backend_atom: RssAtomFeedBackend,
) -> None:
    result = await backend_atom.poll("https://example.com/feed.atom")
    assert len(result.items) == 2
    assert result.items[0].title == "First Story"
    assert result.items[1].title == "Second Story"


async def test_poll_dedup_by_guid_yields_same_uids(
    backend_atom: RssAtomFeedBackend,
) -> None:
    result_a = await backend_atom.poll("https://example.com/feed.atom")
    result_b = await backend_atom.poll("https://example.com/feed.atom")
    assert [i.item_uid for i in result_a.items] == [i.item_uid for i in result_b.items]


async def test_poll_falls_back_to_link_when_no_guid() -> None:
    body = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>NL</title><link>https://x.com/</link>
<description>x</description>
<item><title>One</title><link>https://x.com/one</link><description>d</description></item>
</channel></rss>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    result = await backend.poll("https://x.com/feed")
    assert len(result.items) == 1
    assert result.items[0].item_uid == "https://x.com/one"


async def test_poll_falls_back_to_hash_when_no_guid_or_link() -> None:
    body = _load("rss_no_guid.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    result = await backend.poll("https://example.com/no-guid.xml")
    assert len(result.items) == 1
    assert result.items[0].item_uid.startswith("hash:")


async def test_poll_returns_not_modified_on_304() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, headers={"etag": '"xyz"'})

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    result = await backend.poll(
        "https://example.com/feed", http_cache={"etag": '"xyz"'}
    )
    assert result.not_modified is True
    assert result.items == []
    assert result.status_code == 304


async def test_etag_round_trip_in_http_cache(backend_atom: RssAtomFeedBackend) -> None:
    result = await backend_atom.poll("https://example.com/feed.atom")
    assert result.http_cache["etag"] == '"abc-123"'
    assert (
        result.http_cache["last_modified"] == "Fri, 01 May 2026 12:00:00 GMT"
    )


async def test_probe_rejects_non_http_url() -> None:
    backend = RssAtomFeedBackend()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    with pytest.raises(FeedError):
        await backend.probe("ftp://example.com/feed")


async def test_basic_auth_header_sent() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, content=_load("atom_basic.xml"))

    backend = RssAtomFeedBackend()
    await backend.initialize(
        {"basic_auth_user": "user", "basic_auth_password": "pw"},
        http_client=httpx.AsyncClient(transport=_make_transport(handler)),
    )
    await backend.probe("https://example.com/private.atom")
    assert captured["auth"].startswith("Basic ")


async def test_required_headers_sent() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("user-agent", "")
        captured["accept"] = request.headers.get("accept", "")
        captured["accept_enc"] = request.headers.get("accept-encoding", "")
        return httpx.Response(200, content=_load("atom_basic.xml"))

    backend = RssAtomFeedBackend()
    await backend.initialize(
        {"user_agent": "Hello/1.0"},
        http_client=httpx.AsyncClient(transport=_make_transport(handler)),
    )
    await backend.probe("https://example.com/feed.atom")
    assert "Hello/1.0" in captured["ua"]
    assert "atom+xml" in captured["accept"]
    assert "gzip" in captured["accept_enc"]


async def test_response_size_cap_aborts_large_body() -> None:
    big_body = b"<rss><channel><title>x</title>" + b"x" * (11 * 1024 * 1024) + b"</channel></rss>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=big_body, headers={"content-length": str(len(big_body))}
        )

    backend = RssAtomFeedBackend()
    await backend.initialize(
        {"max_response_bytes": 10 * 1024 * 1024},
        http_client=httpx.AsyncClient(transport=_make_transport(handler)),
    )
    with pytest.raises(FeedTooLargeError):
        await backend.poll("https://example.com/big.xml")


async def test_unknown_content_encoding_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<rss/>",
            headers={"content-encoding": "br"},
        )

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    with pytest.raises(FeedError):
        await backend.poll("https://example.com/br.xml")


async def test_https_to_http_redirect_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301, headers={"location": "http://example.com/insecure"}
        )

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    with pytest.raises(FeedError):
        await backend.poll("https://example.com/feed.xml")


async def test_redirect_chain_capped_at_five() -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(
            302, headers={"location": f"https://example.com/r{counter['n']}"}
        )

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    with pytest.raises(FeedError):
        await backend.poll("https://example.com/start")


async def test_atom_feed_with_id_only_no_link() -> None:
    body = _load("atom_no_link.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    result = await backend.poll("https://example.com/no-link.atom")
    assert len(result.items) == 1
    assert result.items[0].item_uid == "tag:example.com,2026:idonly"


async def test_malformed_xml_handled_gracefully() -> None:
    body = b"<rss><channel><title>broken"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    # feedparser is forgiving — just verify we don't crash.
    result = await backend.poll("https://example.com/bad.xml")
    assert isinstance(result.items, list)


async def test_ttl_clamps_effective_interval_upward(backend_rss: RssAtomFeedBackend) -> None:
    result = await backend_rss.poll("https://example.com/rss.xml")
    # ttl=60 minutes → 3600 seconds.
    assert result.suggested_min_interval_sec >= 3600


async def test_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    with pytest.raises(FeedAuthError):
        await backend.poll("https://example.com/private.xml")


async def test_empty_feed_no_items_no_error() -> None:
    body = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title><link>https://x.com/</link><description>none</description></channel></rss>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    backend = RssAtomFeedBackend()
    await backend.initialize({}, http_client=httpx.AsyncClient(transport=_make_transport(handler)))
    result = await backend.poll("https://example.com/empty.xml")
    assert result.items == []

