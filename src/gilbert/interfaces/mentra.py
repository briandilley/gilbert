"""Mentra smart-glasses platform interface.

Mentra (https://mentra.glass) is a manufacturer-agnostic smart-glasses
OS — the same app runs across Even Realities G1, Vuzix Z100, Mentra
Live, and future devices. Unlike the carrier interfaces (telephony,
messaging) where a third-party provider initiates calls to OUR app,
Mentra's protocol is inverted:

1. **Mentra Cloud POSTs a webhook** (``session_request``) to OUR app
   when a user launches the app from their phone. The webhook payload
   carries ``sessionId``, ``userId`` (email), and ``websocketUrl``.
2. **OUR app dials back to that WebSocket URL** and sends
   ``tpa_connection_init`` as the first frame. The cloud responds with
   ``tpa_connection_ack`` carrying initial settings + device
   capabilities.
3. Bidirectional JSON-over-WS thereafter. Inbound stream events
   (transcription, button press, IMU, location, …) wrapped in a
   ``data_stream`` envelope; outbound commands (display, TTS, camera,
   LED, dashboard) are top-level typed messages.
4. Binary WS frames carry raw PCM audio in both directions (16 kHz
   mono 16-bit).

This module declares the WEBHOOK side only — the inbound HTTP route
that core's ``/api/mentra/webhook`` mounts. The full session protocol
(WebSocket client, message routing, manager objects) lives in the
plugin (``std-plugins/mentra/``) because it ships a substantial chunk
of TypeScript-SDK-derived logic that doesn't belong in core
``interfaces/``.

Mirrors the ``TelnyxWebhookEndpoint`` / ``MessagingWebhookEndpoint``
pattern: the core route stays plugin-agnostic, the plugin advertises
the ``mentra_webhook`` capability and parses the JSON.

Pure interfaces module: stdlib + dataclasses only. No HTTP client,
no plugin imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "AudioChunk",
    "ButtonPress",
    "GlassesCapabilities",
    "HeadPosition",
    "LocationData",
    "LocationUpdate",
    "MentraWebhookEndpoint",
    "PhotoData",
    "SessionWebhookRequest",
    "StopRequestReason",
    "StopWebhookRequest",
    "StreamResult",
    "TranscriptionData",
    "VadEvent",
    "WebhookRequestType",
    "WebhookResponse",
]


class WebhookRequestType(StrEnum):
    """Type discriminator on inbound Mentra webhooks.

    Mentra Cloud uses the same endpoint for both lifecycle events;
    the ``type`` field decides which dataclass to deserialize into.
    """

    SESSION_REQUEST = "session_request"
    STOP_REQUEST = "stop_request"


class StopRequestReason(StrEnum):
    """Why Mentra Cloud is asking us to stop the session.

    Distinguishes "user pressed stop" from "the system terminated
    us" so the plugin can react differently (e.g. play a goodbye
    chime on user_disabled, silently shut down on system_stop).
    """

    USER_DISABLED = "user_disabled"
    SYSTEM_STOP = "system_stop"
    ERROR = "error"


@dataclass
class SessionWebhookRequest:
    """Inbound webhook fired when a user launches the Mentra app.

    ``sessionId`` is the cloud's correlation id — we echo it back in
    the WebSocket handshake so the cloud can route per-user-session.
    ``userId`` is the user's email address (Mentra's identity model).
    ``websocket_url`` is where we dial back to open the live
    bidirectional channel.

    The two ``*_alias`` fields are deprecated legacy names; recent
    SDK versions emit ``websocket_url`` but the cloud still ships
    the aliases for older apps. We accept whichever is set.
    """

    session_id: str
    user_id: str
    timestamp: str  # ISO 8601 UTC
    websocket_url: str = ""
    mentraos_websocket_url_alias: str = ""  # legacy: ``mentraOSWebsocketUrl``
    augmentos_websocket_url_alias: str = ""  # legacy: ``augmentOSWebsocketUrl``

    @property
    def resolved_websocket_url(self) -> str:
        """Return whichever URL field the cloud populated. Prefers the
        modern ``websocket_url`` and falls back to the deprecated
        aliases per the upstream SDK's resolution order."""
        return (
            self.websocket_url
            or self.mentraos_websocket_url_alias
            or self.augmentos_websocket_url_alias
        )


@dataclass
class StopWebhookRequest:
    """Inbound webhook fired when a user stops the Mentra app or the
    cloud terminates the session.

    ``reason`` distinguishes user-initiated stops from system-initiated
    ones — the plugin uses it to decide whether to play a farewell
    cue or shut down quietly."""

    session_id: str
    user_id: str
    timestamp: str  # ISO 8601 UTC
    reason: str = StopRequestReason.SYSTEM_STOP.value


@dataclass
class WebhookResponse:
    """Body the app returns from its webhook endpoint. Mentra Cloud
    treats ``status="error"`` as the session-setup-failed signal."""

    status: str = "success"  # "success" | "error"
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        out: dict[str, str] = {"status": self.status}
        if self.message:
            out["message"] = self.message
        return out


@runtime_checkable
class MentraWebhookEndpoint(Protocol):
    """Capability advertised by the Mentra plugin so core's
    ``/api/mentra/webhook`` + ``/api/mentra/photo-upload`` routes can
    hand the raw payloads off without importing the plugin module
    directly.

    The plugin:

    - Parses lifecycle webhook payloads into ``SessionWebhookRequest``
      or ``StopWebhookRequest`` and, for session requests, opens a
      WebSocket back to Mentra Cloud and binds the per-session
      managers (``deliver_webhook_event``).
    - Resolves inbound photo uploads against the matching pending
      ``CameraManager.take_photo()`` future
      (``deliver_photo_upload``). Mentra Cloud POSTs photo bytes here
      after the ASG glasses snap the photo — the request_id matches
      what we sent in the original ``photo_request`` frame.

    Mirrors ``TelnyxWebhookEndpoint`` / ``MessagingWebhookEndpoint``
    for parallelism — the route's contract is the same regardless of
    which third-party platform is on the far end.
    """

    async def deliver_webhook_event(
        self, payload: dict[str, object]
    ) -> WebhookResponse:
        ...

    async def deliver_photo_upload(
        self,
        *,
        request_id: str,
        photo_bytes: bytes,
        mime_type: str,
        error_code: str = "",
        error_message: str = "",
    ) -> WebhookResponse:
        """Resolve a pending photo request with the cloud's uploaded
        bytes (or a failure signal).

        - ``request_id`` matches the ``requestId`` field on the
          original ``photo_request`` WS frame.
        - ``photo_bytes`` is empty on the error path
          (``error_code`` / ``error_message`` populated).
        - ``mime_type`` from the upload's ``Content-Type`` (typically
          ``image/jpeg``; cloud may send PNG depending on settings).

        Returns ``status=success`` when a pending request matched +
        was resolved, ``status=error`` when no pending request was
        found (e.g. the take_photo() call already timed out). The
        plugin treats both cases as 200 so the cloud doesn't retry.
        """
        ...


@runtime_checkable
class MentraDebugProvider(Protocol):
    """Capability advertised by the Mentra plugin so core's
    ``/api/mentra/debug/*`` routes can introspect live session state
    without importing the plugin module directly.

    Exposes a per-user ring buffer of recent events (session lifecycle
    transitions, transcription finals, AI dispatches, audio
    responses, etc.) used by the in-glasses-app companion webview
    for live debugging. The webview reads ``aos_signed_user_token``
    from the URL, decodes the JWT payload's ``sub`` claim to identify
    the user, and polls this provider for that user's recent events.
    """

    def get_recent_events(
        self, mentra_user_id: str, *, limit: int = 50
    ) -> list[dict[str, object]]:
        """Return up to ``limit`` recent events for the given Mentra
        user, oldest first. Each event is a dict with at minimum
        ``timestamp`` (ISO 8601), ``kind`` (event-type string),
        ``level`` (``"info"`` | ``"warning"`` | ``"error"``), and
        ``message`` (display string). Some events carry extra
        ``data``."""
        ...

    def get_active_session_summary(
        self, mentra_user_id: str
    ) -> dict[str, object] | None:
        """Snapshot of the currently-live session for the user, or
        ``None`` if no session is active. Used by the debug webview
        for the "connected device" header."""
        ...


# ── Subscription-side data classes ──────────────────────────────────
#
# These appear in the plugin too, but live here so any future service
# that wants to react to Mentra events (e.g. a presence service that
# treats "user is wearing their glasses" as a presence signal) can do
# so without depending on plugin-internal types.


@dataclass
class TranscriptionData:
    """One transcription frame surfaced from glasses → cloud → app.

    ``is_final`` distinguishes the in-progress hypothesis (Mentra
    streams partials for low-latency UI) from the committed text the
    AI should actually act on. The recommended rule is to gate
    command execution on ``is_final`` and use partials only for
    visual feedback.
    """

    text: str
    is_final: bool
    transcribe_language: str = ""  # e.g. "en-US"
    confidence: float = 0.0
    start_time: float = 0.0  # ms since session start
    end_time: float = 0.0
    speaker_id: str = ""
    duration: float = 0.0


@dataclass
class ButtonPress:
    """Physical button event from the glasses. ``press_type`` is one
    of ``"short"`` / ``"long"`` per the upstream SDK's
    ``ButtonPress`` interface."""

    button_id: str
    press_type: str = "short"  # "short" | "long"


@dataclass
class HeadPosition:
    """IMU-derived head orientation. Mentra reports the discrete
    bucket the user is currently looking in — ``"up"`` is what
    triggers the dashboard mode."""

    position: str  # "up" | "down"


@dataclass
class LocationUpdate:
    """GPS coordinates from the phone (the glasses don't have a GPS
    chip of their own). Accuracy and altitude are NOT in the
    upstream SDK's minimal schema — only lat/lng — so anything
    derived from accuracy tiers happens cloud-side before we see
    the event."""

    lat: float
    lng: float


@dataclass
class AudioChunk:
    """One chunk of raw PCM audio from the glasses microphone.

    The wire format is always 16 kHz mono signed 16-bit PCM — the
    native format of the glasses mic hardware. Arrives over the
    WebSocket as binary frames; the session layer wraps each frame
    with metadata before surfacing to handlers."""

    data: bytes
    sample_rate: int = 16000
    channels: int = 1
    timestamp_ms: float = 0.0


@dataclass
class VadEvent:
    """Voice-activity detection state from the glasses.

    The glasses run on-device VAD and emit start/stop events as the
    user's speech state changes. ``is_speaking=True`` when speech
    began; ``False`` when it ended. The cloud sometimes ships the
    ``status`` field as a string (``"true"`` / ``"false"``) rather
    than a JSON boolean — the manager normalizes both shapes."""

    is_speaking: bool
    timestamp_ms: float = 0.0


@dataclass
class LocationData:
    """Resolved location from the glasses-via-phone GPS.

    Mirrors the upstream SDK's ``LocationData`` interface — only
    lat/lng are guaranteed; accuracy may be absent on degraded
    fixes. ``correlation_id`` is set when the data is the response
    to a one-shot ``request_update()`` call (the manager uses it to
    resolve the matching pending Future)."""

    lat: float
    lng: float
    accuracy: float | None = None
    timestamp_ms: float = 0.0
    correlation_id: str = ""


@dataclass
class PhotoData:
    """Result of a successful ``CameraManager.take_photo()`` call.

    Mentra Cloud delivers the actual photo via TWO patterns depending
    on the device + cloud configuration:

    1. **HTTP push (Mentra Live default)** — Cloud POSTs multipart
       form-data to ``<app-server>/api/mentra/photo-upload`` with the
       file bytes inline. The plugin's photo-upload handler stuffs
       ``data`` + ``mime_type`` directly into this dataclass and
       resolves the pending ``take_photo()`` future. ``url`` stays
       empty in this case.
    2. **Cloud-hosted URL (legacy / some devices)** — Cloud responds
       to the WS ``photo_request`` with a ``photo_response`` carrying
       a ``photoUrl``. Caller downloads bytes from the URL itself.
       ``data`` + ``mime_type`` stay empty.

    Consumers should prefer ``data`` when present (zero round-trip)
    and only fall back to ``url`` when it's not.
    """

    url: str = ""
    data: bytes = b""
    mime_type: str = ""
    width: int = 0
    height: int = 0
    timestamp_ms: float = 0.0
    saved_to_gallery: bool = False
    request_id: str = ""


@dataclass
class StreamResult:
    """URLs returned when a managed livestream goes active.

    ``hls_url`` / ``dash_url`` / ``webrtc_url`` are viewer-facing
    playback endpoints (HLS is the most broadly compatible).
    ``preview_url`` / ``thumbnail_url`` are static images for
    cards. ``stream_id`` is the cloud's correlation handle —
    useful for diagnostics."""

    hls_url: str = ""
    dash_url: str = ""
    webrtc_url: str = ""
    preview_url: str = ""
    thumbnail_url: str = ""
    stream_id: str = ""


@dataclass
class GlassesCapabilities:
    """Hardware advertisement the cloud sends in the connection
    ack frame. The plugin branches on these to decide which
    managers do anything useful (e.g. ``CameraManager`` only
    activates on glasses with ``has_camera=True``).

    Field names match the upstream ``Capabilities`` interface, with
    Python snake_case translation."""

    model_name: str = ""
    has_camera: bool = False
    has_display: bool = False
    has_microphone: bool = False
    has_speaker: bool = False
    has_imu: bool = False
    has_button: bool = False
    has_light: bool = False
    has_wifi: bool = False
    raw: dict[str, object] = field(default_factory=dict)  # full original payload
