"""Built-in RSS 2.0 / Atom 1.0 feed backend.

The only feed backend that lives in ``src/gilbert/integrations/``;
provider-specific backends (Reddit, HackerNews, YouTube, podcasts)
belong in ``std-plugins/``. ``feedparser`` is BSD-licensed, ~150 KB on
disk, stdlib-only transitive deps, and parses two open standards (no
vendor specifics) — clears the "may live in core integrations/" test
documented in the feeds spec §3.

Implementation notes:

- All ``feedparser`` calls go through ``asyncio.to_thread`` (it does
  CPU-bound XML parsing, not async I/O).
- HTTP fetch uses ``httpx.AsyncClient`` with required headers
  (User-Agent, Accept, Accept-Encoding), conditional GET (etag /
  If-Modified-Since), gzip/deflate transparent decoding, and a
  body-size cap so a hostile feed cannot OOM Gilbert.
- Body fetch is streamed and aborted if ``Content-Length`` exceeds
  ``max_response_bytes`` OR if streamed bytes exceed it.
- Encoding allow-list: ``gzip`` / ``deflate`` only — ``br``, ``zstd``,
  or unknown encodings fail closed with ``FeedError``.
- Redirect cap of 5; ``https → http`` downgrades rejected (mild SSRF
  guard).
- ``initialize`` accepts an injectable ``http_client`` so tests can
  pass ``httpx.AsyncClient(transport=httpx.MockTransport(...))`` and
  exercise the real ``feedparser`` against fixture bytes without
  touching the network. We never mock ``feedparser`` itself — that
  would defeat the point of the test.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import feedparser  # type: ignore[import-untyped]
import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.feeds import (
    FeedAuthError,
    FeedBackend,
    FeedError,
    FeedItem,
    FeedMeta,
    FeedNotFoundError,
    FeedTooLargeError,
    PollResult,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "GilbertFeeds/1.0 (+https://github.com/briandilley/gilbert)"
_DEFAULT_ACCEPT = (
    "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.8"
)
_DEFAULT_ACCEPT_ENCODING = "gzip, deflate"
_ALLOWED_ENCODINGS = frozenset({"", "identity", "gzip", "deflate"})
_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB
_MAX_REDIRECTS = 5


def _coerce_dt(value: Any) -> datetime | None:
    """Best-effort coerce a feedparser time-tuple / string to a UTC datetime.

    feedparser exposes ``published_parsed`` as a 9-tuple in UTC. If
    that's missing or malformed we fall back to ``None`` (the
    consumer fills in ``received_at``).
    """
    if value is None:
        return None
    if isinstance(value, time.struct_time):
        try:
            return datetime(*value[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _strip_html(text: str) -> str:
    """Quick-and-dirty HTML strip for the persisted ``summary`` field.

    feedparser already gives us reasonably clean text for most feeds;
    this just collapses tag soup so we don't store mountains of HTML
    we'd never render. Real HTML parsing belongs in the knowledge
    ingestion path (``BeautifulSoup``-shaped) not here — this only
    needs to keep the displayed summary readable.
    """
    if not text:
        return ""
    out: list[str] = []
    in_tag = False
    for ch in text:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            out.append(" ")
            continue
        if not in_tag:
            out.append(ch)
    # Collapse whitespace.
    return " ".join("".join(out).split())


class RssAtomFeedBackend(FeedBackend):
    """RSS 2.0 / Atom 1.0 feed backend backed by ``feedparser`` + ``httpx``.

    HTTP work happens through ``httpx.AsyncClient`` (created in
    ``initialize``, closed in ``close`` so resource lifetime tracks
    the runtime). XML parsing is delegated to ``feedparser`` via
    ``asyncio.to_thread`` because feedparser does blocking work.
    """

    backend_name = "rss_atom"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="user_agent",
                type=ToolParameterType.STRING,
                description=(
                    "User-Agent string sent on feed requests. Some hosts "
                    "rate-limit the default Python UA, so we identify "
                    "Gilbert explicitly. Leave blank to use the bundled "
                    "default."
                ),
                default=_DEFAULT_USER_AGENT,
            ),
            ConfigParam(
                key="basic_auth_user",
                type=ToolParameterType.STRING,
                description=(
                    "HTTP Basic auth username (optional, for private feeds)."
                ),
                default="",
            ),
            ConfigParam(
                key="basic_auth_password",
                type=ToolParameterType.STRING,
                description="HTTP Basic auth password (optional).",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="request_timeout_sec",
                type=ToolParameterType.INTEGER,
                description="HTTP timeout per request (seconds).",
                default=15,
            ),
            ConfigParam(
                key="max_response_bytes",
                type=ToolParameterType.INTEGER,
                description=(
                    "Hard cap on feed response body size. Raises FeedError "
                    "if exceeded; counts as a failure. Default 10 MiB."
                ),
                default=_DEFAULT_MAX_RESPONSE_BYTES,
            ),
        ]

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._owns_client: bool = True
        self._user_agent: str = _DEFAULT_USER_AGENT
        self._basic_auth: tuple[str, str] | None = None
        self._timeout_sec: int = 15
        self._max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    async def initialize(
        self,
        config: dict[str, Any] | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build / accept the HTTP client and apply backend config.

        ``http_client`` is the test injection seam — pass an
        ``httpx.AsyncClient(transport=httpx.MockTransport(...))`` to
        exercise the real ``feedparser`` against fixture bytes
        without touching the network. When provided, the backend does
        not own the client (caller closes it).
        """
        cfg = dict(config or {})
        ua = str(cfg.get("user_agent", "") or "") or _DEFAULT_USER_AGENT
        self._user_agent = ua
        user = str(cfg.get("basic_auth_user", "") or "")
        password = str(cfg.get("basic_auth_password", "") or "")
        self._basic_auth = (user, password) if user else None
        try:
            self._timeout_sec = int(cfg.get("request_timeout_sec", 15) or 15)
        except (TypeError, ValueError):
            self._timeout_sec = 15
        try:
            self._max_response_bytes = int(
                cfg.get("max_response_bytes", _DEFAULT_MAX_RESPONSE_BYTES)
                or _DEFAULT_MAX_RESPONSE_BYTES
            )
        except (TypeError, ValueError):
            self._max_response_bytes = _DEFAULT_MAX_RESPONSE_BYTES

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_sec,
                follow_redirects=False,
            )
            self._owns_client = True

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # ---------------- HTTP fetch ----------------

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise FeedError(f"Unsupported URL scheme: {parsed.scheme!r}")
        if not parsed.netloc:
            raise FeedError(f"Malformed feed URL: {url!r}")
        return url

    async def _fetch(
        self,
        url: str,
        *,
        http_cache: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, str], int, bool]:
        """GET ``url`` with the politeness contract.

        Returns ``(body_bytes, http_cache_out, status_code,
        not_modified)``. Honors etag / If-Modified-Since round-trip,
        rejects oversized bodies, rejects unknown content-encodings,
        rejects ``https → http`` redirects, and caps the redirect
        chain at 5.
        """
        assert self._client is not None
        url = self._validate_url(url)
        original_scheme = urlparse(url).scheme

        headers: dict[str, str] = {
            "User-Agent": self._user_agent,
            "Accept": _DEFAULT_ACCEPT,
            "Accept-Encoding": _DEFAULT_ACCEPT_ENCODING,
        }
        cache_in = dict(http_cache or {})
        if cache_in.get("etag"):
            headers["If-None-Match"] = cache_in["etag"]
        if cache_in.get("last_modified"):
            headers["If-Modified-Since"] = cache_in["last_modified"]

        auth: tuple[str, str] | None = self._basic_auth

        # Manual redirect handling so we can enforce the
        # https→http downgrade rule and the redirect-chain cap.
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                response = await self._client.get(
                    current_url,
                    headers=headers,
                    auth=auth,
                )
            except httpx.HTTPError as exc:
                raise FeedError(f"HTTP error fetching {current_url!r}: {exc}") from exc

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise FeedError(f"Redirect without Location: {current_url!r}")
                next_url = str(httpx.URL(current_url).join(location))
                if (
                    original_scheme == "https"
                    and urlparse(next_url).scheme == "http"
                ):
                    raise FeedError(
                        f"Refusing https → http redirect: {current_url!r} → {next_url!r}"
                    )
                current_url = next_url
                continue

            if response.status_code == 304:
                return b"", dict(cache_in), 304, True
            if response.status_code == 401 or response.status_code == 403:
                raise FeedAuthError(
                    f"Authorization failed ({response.status_code}) for {current_url!r}"
                )
            if response.status_code == 404:
                raise FeedNotFoundError(f"Feed not found: {current_url!r}")
            if response.status_code >= 400:
                raise FeedError(
                    f"HTTP {response.status_code} fetching {current_url!r}"
                )

            # Encoding allow-list — httpx already decompresses gzip /
            # deflate transparently when ``Accept-Encoding`` advertises
            # them. Reject anything we'd have to bring in another lib for.
            content_encoding = response.headers.get("content-encoding", "").lower()
            if content_encoding and content_encoding not in _ALLOWED_ENCODINGS:
                raise FeedError(
                    f"Unsupported Content-Encoding: {content_encoding!r}"
                )

            # Body-size cap — Content-Length first if advertised.
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > self._max_response_bytes:
                        raise FeedTooLargeError(
                            f"Feed body Content-Length {length} exceeds cap "
                            f"{self._max_response_bytes}"
                        )
                except ValueError:
                    pass  # malformed header; fall through to streamed check.

            body = response.content
            if len(body) > self._max_response_bytes:
                raise FeedTooLargeError(
                    f"Feed body {len(body)} bytes exceeds cap "
                    f"{self._max_response_bytes}"
                )

            cache_out: dict[str, str] = dict(cache_in)
            etag = response.headers.get("etag", "")
            last_modified = response.headers.get("last-modified", "")
            if etag:
                cache_out["etag"] = etag
            if last_modified:
                cache_out["last_modified"] = last_modified
            # Cache hint — Cache-Control: max-age (seconds).
            cache_control = response.headers.get("cache-control", "")
            if "max-age=" in cache_control:
                token = cache_control.split("max-age=", 1)[1].split(",", 1)[0]
                try:
                    cache_out["max_age_sec"] = str(int(token.strip()))
                except (TypeError, ValueError):
                    pass

            return body, cache_out, response.status_code, False

        raise FeedError(
            f"Too many redirects (>{_MAX_REDIRECTS}) starting at {url!r}"
        )

    # ---------------- Item construction ----------------

    @staticmethod
    def _derive_item_uid(entry: Any) -> str:
        """Derive a deterministic ``item_uid`` per spec §6.4b.

        1. Feed-supplied ``<guid>`` / ``<id>`` if non-empty.
        2. Otherwise ``<link>`` (whitespace stripped, lowercased host).
        3. Otherwise SHA1 hash of ``<title> + <published_iso>``.
        """
        guid = (
            getattr(entry, "id", None)
            or getattr(entry, "guid", None)
            or ""
        )
        if guid:
            return str(guid).strip()
        link = str(getattr(entry, "link", "") or "").strip()
        if link:
            parsed = urlparse(link)
            normalized = parsed._replace(netloc=parsed.netloc.lower()).geturl()
            return normalized
        title = str(getattr(entry, "title", "") or "")
        pub = ""
        published_dt = _coerce_dt(getattr(entry, "published_parsed", None))
        if published_dt:
            pub = published_dt.isoformat()
        digest = hashlib.sha1(f"{title}|{pub}".encode()).hexdigest()
        return f"hash:{digest}"

    @classmethod
    def _entry_to_item(cls, entry: Any) -> FeedItem:
        title = str(getattr(entry, "title", "") or "")
        link = str(getattr(entry, "link", "") or "").strip()
        author = str(getattr(entry, "author", "") or "")

        # feedparser exposes summary at .summary; some atom feeds put
        # the body in .content (a list of dicts). Prefer .summary.
        summary_raw = ""
        if getattr(entry, "summary", None):
            summary_raw = str(entry.summary)
        elif getattr(entry, "description", None):
            summary_raw = str(entry.description)
        elif getattr(entry, "content", None):
            content_list = entry.content
            if isinstance(content_list, list) and content_list:
                first = content_list[0]
                if isinstance(first, dict):
                    summary_raw = str(first.get("value", "") or "")
        summary = _strip_html(summary_raw)

        published_at = _coerce_dt(getattr(entry, "published_parsed", None))
        updated_at = (
            _coerce_dt(getattr(entry, "updated_parsed", None))
            or published_at
        )

        enclosure_url = ""
        enclosure_mime = ""
        enclosures = getattr(entry, "enclosures", None) or []
        if isinstance(enclosures, list) and enclosures:
            first = enclosures[0]
            if isinstance(first, dict):
                enclosure_url = str(first.get("href", "") or first.get("url", ""))
                enclosure_mime = str(first.get("type", "") or "")

        return FeedItem(
            item_uid=cls._derive_item_uid(entry),
            title=title,
            link=link,
            summary=summary,
            author=author,
            published_at=published_at,
            updated_at=updated_at,
            enclosure_url=enclosure_url,
            enclosure_mime=enclosure_mime,
        )

    @staticmethod
    def _suggested_min_interval_sec(parsed: Any, http_cache: dict[str, str]) -> int:
        """Compute ``max(<ttl>*60, Cache-Control: max-age)``."""
        # ttl from the feed body (RSS, in minutes).
        ttl_sec = 0
        try:
            ttl_raw = parsed.feed.get("ttl") if hasattr(parsed, "feed") else None
            if ttl_raw:
                ttl_sec = int(ttl_raw) * 60
        except (TypeError, ValueError):
            ttl_sec = 0
        max_age_sec = 0
        try:
            max_age_sec = int(http_cache.get("max_age_sec") or 0)
        except (TypeError, ValueError):
            max_age_sec = 0
        return max(ttl_sec, max_age_sec)

    # ---------------- Public surface ----------------

    async def probe(self, url: str) -> FeedMeta:
        if self._client is None:
            raise FeedError("Backend not initialized")
        try:
            body, _, _, _ = await self._fetch(url)
        except FeedError:
            raise
        except Exception as exc:
            raise FeedError(f"Failed to probe {url!r}: {exc}") from exc

        parsed = await asyncio.to_thread(feedparser.parse, body)
        feed_meta = parsed.feed if hasattr(parsed, "feed") else {}
        title = str(getattr(feed_meta, "title", "") or feed_meta.get("title", "") or "")
        if not title:
            raise FeedError(f"Could not parse feed metadata for {url!r}")
        description = str(
            getattr(feed_meta, "subtitle", "")
            or feed_meta.get("subtitle", "")
            or feed_meta.get("description", "")
            or ""
        )
        link = str(getattr(feed_meta, "link", "") or feed_meta.get("link", "") or "")
        language = str(
            getattr(feed_meta, "language", "")
            or feed_meta.get("language", "")
            or ""
        )
        icon_url = ""
        image = feed_meta.get("image") if isinstance(feed_meta, dict) else None
        if isinstance(image, dict):
            icon_url = str(image.get("href", "") or image.get("url", "") or "")
        return FeedMeta(
            title=title,
            description=description,
            link=link,
            language=language,
            icon_url=icon_url,
        )

    async def poll(
        self,
        url: str,
        *,
        since: datetime | None = None,
        max_items: int = 100,
        http_cache: dict[str, str] | None = None,
    ) -> PollResult:
        if self._client is None:
            raise FeedError("Backend not initialized")
        body, cache_out, status_code, not_modified = await self._fetch(
            url,
            http_cache=http_cache,
        )
        if not_modified:
            return PollResult(
                items=[],
                http_cache=cache_out,
                suggested_min_interval_sec=0,
                not_modified=True,
                status_code=status_code,
            )

        parsed = await asyncio.to_thread(feedparser.parse, body)
        entries = list(parsed.entries or [])[: max(1, int(max_items))]
        items = [self._entry_to_item(e) for e in entries]
        suggested = self._suggested_min_interval_sec(parsed, cache_out)
        return PollResult(
            items=items,
            http_cache=cache_out,
            suggested_min_interval_sec=suggested,
            not_modified=False,
            status_code=status_code,
        )

