"""Spotify music backend — search, metadata, and playable URIs via the Spotify Web API."""

import logging
import time
from typing import Any

import httpx

from gilbert.interfaces.music import (
    AlbumInfo,
    ArtistInfo,
    MusicBackend,
    PlaylistDetail,
    PlaylistInfo,
    SearchResults,
    TrackInfo,
)

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API_BASE = "https://api.spotify.com/v1"


def _parse_artists(raw: list[dict[str, Any]]) -> list[ArtistInfo]:
    """Parse artist objects from Spotify API response."""
    return [
        ArtistInfo(
            artist_id=a["id"],
            name=a["name"],
            external_url=a.get("external_urls", {}).get("spotify", ""),
        )
        for a in raw
    ]


def _parse_album(raw: dict[str, Any]) -> AlbumInfo:
    """Parse an album object from Spotify API response."""
    images = raw.get("images", [])
    return AlbumInfo(
        album_id=raw["id"],
        name=raw["name"],
        artists=_parse_artists(raw.get("artists", [])),
        album_art_url=images[0]["url"] if images else "",
        release_date=raw.get("release_date", ""),
        total_tracks=raw.get("total_tracks", 0),
        external_url=raw.get("external_urls", {}).get("spotify", ""),
    )


def _parse_track(raw: dict[str, Any]) -> TrackInfo:
    """Parse a track object from Spotify API response."""
    album_data = raw.get("album")
    return TrackInfo(
        track_id=raw["id"],
        name=raw["name"],
        artists=_parse_artists(raw.get("artists", [])),
        album=_parse_album(album_data) if album_data else None,
        duration_seconds=raw.get("duration_ms", 0) / 1000.0,
        track_number=raw.get("track_number", 0),
        uri=raw.get("uri", ""),
        external_url=raw.get("external_urls", {}).get("spotify", ""),
        preview_url=raw.get("preview_url") or "",
        explicit=raw.get("explicit", False),
    )


def _parse_playlist(raw: dict[str, Any]) -> PlaylistInfo:
    """Parse a playlist object from Spotify API response."""
    images = raw.get("images", [])
    owner = raw.get("owner", {})
    return PlaylistInfo(
        playlist_id=raw["id"],
        name=raw["name"],
        description=raw.get("description", ""),
        owner=owner.get("display_name", owner.get("id", "")),
        track_count=raw.get("tracks", {}).get("total", 0),
        external_url=raw.get("external_urls", {}).get("spotify", ""),
        image_url=images[0]["url"] if images else "",
    )


class SpotifyMusic(MusicBackend):
    """Spotify music backend using the Spotify Web API with client credentials."""

    backend_name = "spotify"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="client_id", type=ToolParameterType.STRING,
                description="Spotify OAuth client ID.",
                sensitive=True, restart_required=True,
            ),
            ConfigParam(
                key="client_secret", type=ToolParameterType.STRING,
                description="Spotify OAuth client secret.",
                sensitive=True, restart_required=True,
            ),
        ]

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_id: str = ""
        self._client_secret: str = ""
        self._access_token: str = ""
        self._token_expires_at: float = 0.0

    async def initialize(self, config: dict[str, object]) -> None:
        client_id = config.get("client_id")
        client_secret = config.get("client_secret")
        if not client_id or not isinstance(client_id, str):
            raise ValueError("Spotify requires 'client_id' in config")
        if not client_secret or not isinstance(client_secret, str):
            raise ValueError("Spotify requires 'client_secret' in config")

        self._client_id = client_id
        self._client_secret = client_secret
        self._client = httpx.AsyncClient(base_url=_API_BASE, timeout=30.0)

        await self._refresh_token()
        logger.info("Spotify music backend initialized")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- Auth ---

    async def _refresh_token(self) -> None:
        """Obtain or refresh an access token using client credentials flow."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
            response.raise_for_status()
            data = response.json()

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600) - 60
        logger.debug("Spotify token refreshed, expires in %ds", data.get("expires_in", 3600))

    async def _ensure_token(self) -> None:
        """Refresh the token if it's expired or about to expire."""
        if time.time() >= self._token_expires_at:
            await self._refresh_token()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make an authenticated GET request to the Spotify API."""
        client = self._require_client()
        await self._ensure_token()
        response = await client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        response.raise_for_status()
        return response.json()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Spotify not initialized — call initialize() first")
        return self._client

    # --- Search ---

    async def search(self, query: str, *, limit: int = 10) -> SearchResults:
        data = await self._get("/search", {
            "q": query,
            "type": "track,album,playlist",
            "limit": limit,
        })

        tracks = [_parse_track(t) for t in data.get("tracks", {}).get("items", []) if t]
        albums = [_parse_album(a) for a in data.get("albums", {}).get("items", []) if a]
        playlists = [_parse_playlist(p) for p in data.get("playlists", {}).get("items", []) if p]

        return SearchResults(tracks=tracks, albums=albums, playlists=playlists)

    # --- Track info ---

    async def get_track(self, track_id: str) -> TrackInfo | None:
        try:
            data = await self._get(f"/tracks/{track_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return _parse_track(data)

    async def get_album(self, album_id: str) -> AlbumInfo | None:
        try:
            data = await self._get(f"/albums/{album_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return _parse_album(data)

    async def get_album_tracks(self, album_id: str) -> list[TrackInfo]:
        data = await self._get(f"/albums/{album_id}/tracks", {"limit": 50})
        # Album track objects don't include album info, so fetch it
        album = await self.get_album(album_id)
        tracks: list[TrackInfo] = []
        for item in data.get("items", []):
            track = TrackInfo(
                track_id=item["id"],
                name=item["name"],
                artists=_parse_artists(item.get("artists", [])),
                album=album,
                duration_seconds=item.get("duration_ms", 0) / 1000.0,
                track_number=item.get("track_number", 0),
                uri=item.get("uri", ""),
                external_url=item.get("external_urls", {}).get("spotify", ""),
                preview_url=item.get("preview_url") or "",
                explicit=item.get("explicit", False),
            )
            tracks.append(track)
        return tracks

    # --- Playlists ---

    async def get_playlist(self, playlist_id: str) -> PlaylistDetail | None:
        try:
            data = await self._get(f"/playlists/{playlist_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

        playlist = _parse_playlist(data)
        tracks: list[TrackInfo] = []
        for item in data.get("tracks", {}).get("items", []):
            track_data = item.get("track")
            if track_data and track_data.get("id"):
                tracks.append(_parse_track(track_data))

        return PlaylistDetail(playlist=playlist, tracks=tracks)

    # --- Playback URIs ---

    async def get_playable_uri(self, track_id: str) -> str:
        """Return the Spotify URI for a track (e.g., 'spotify:track:xxx').

        Sonos and other smart speakers with native Spotify support can play
        these URIs directly.
        """
        return f"spotify:track:{track_id}"
