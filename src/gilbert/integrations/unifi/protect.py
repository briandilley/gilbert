"""UniFi Protect integration — camera AI detection and face recognition."""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from gilbert.integrations.unifi.client import UniFiClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Camera:
    """A UniFi Protect camera."""

    camera_id: str
    name: str
    model: str
    state: str
    last_motion: int  # epoch ms
    is_doorbell: bool = False


@dataclass(frozen=True)
class DetectionEvent:
    """A smart detection event from Protect."""

    event_id: str
    camera_name: str
    event_type: str
    smart_types: list[str] = field(default_factory=list)
    start: int = 0  # epoch ms
    end: int = 0
    score: int = 0


@dataclass(frozen=True)
class FaceDetection:
    """A face recognition result from Protect."""

    person_name: str
    camera_name: str
    timestamp: int  # epoch ms
    confidence: int = 0


class UniFiProtect:
    """Queries UniFi Protect for camera AI detections and face recognition."""

    def __init__(
        self,
        client: UniFiClient,
        zone_aliases: dict[str, list[str]] | None = None,
    ) -> None:
        self._client = client
        self._zone_aliases: dict[str, list[str]] = zone_aliases or {}
        self._camera_names: dict[str, str] = {}  # id → name, populated lazily

    async def list_cameras(self) -> list[Camera]:
        """List all cameras and their status."""
        data = await self._client.get("/proxy/protect/api/cameras")
        if data is None:
            return []

        cameras: list[Camera] = []
        for c in data if isinstance(data, list) else []:
            feature_flags = c.get("featureFlags", {}) or {}
            model_name = c.get("type", "")
            is_doorbell = (
                feature_flags.get("hasChime", False)
                or "doorbell" in model_name.lower()
            )
            cameras.append(Camera(
                camera_id=c.get("id", ""),
                name=c.get("name", ""),
                model=model_name,
                state=c.get("state", ""),
                last_motion=c.get("lastMotion", 0),
                is_doorbell=is_doorbell,
            ))
        return cameras

    async def _ensure_camera_names(self) -> None:
        """Populate camera ID → name map if empty."""
        if self._camera_names:
            return
        cameras = await self.list_cameras()
        self._camera_names = {c.camera_id: c.name for c in cameras if c.camera_id}

    def _resolve_camera_name(self, camera_field: Any) -> str:
        """Resolve a camera name from an event's camera field.

        The field may be a dict with 'name', a camera ID string, or missing.
        """
        if isinstance(camera_field, dict):
            return camera_field.get("name", "")
        if isinstance(camera_field, str) and camera_field:
            return self._camera_names.get(camera_field, camera_field)
        return ""

    async def get_detection_events(
        self,
        lookback_minutes: int = 30,
        event_types: list[str] | None = None,
    ) -> list[DetectionEvent]:
        """Get smart detection events within the lookback window."""
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback_minutes * 60 * 1000)

        types = event_types or ["smartDetectZone", "smartDetectLine"]
        params: dict[str, Any] = {
            "start": start_ms,
            "end": now_ms,
            "types": types,
        }

        data = await self._client.get("/proxy/protect/api/events", params=params)
        if data is None:
            return []

        # Ensure camera ID→name map is populated for resolving IDs
        await self._ensure_camera_names()

        events: list[DetectionEvent] = []
        for e in data if isinstance(data, list) else []:
            events.append(DetectionEvent(
                event_id=e.get("id", ""),
                camera_name=self._resolve_camera_name(e.get("camera")),
                event_type=e.get("type", ""),
                smart_types=e.get("smartDetectTypes", []),
                start=e.get("start", 0),
                end=e.get("end", 0),
                score=e.get("score", 0),
            ))
        return events

    async def get_face_detections(self, lookback_minutes: int = 30) -> list[FaceDetection]:
        """Get face recognition results within the lookback window.

        Faces must be named in the Protect UI's Recognition tab to be matched.
        """
        events = await self.get_detection_events(
            lookback_minutes=lookback_minutes,
            event_types=["smartDetectZone", "smartDetectLine"],
        )

        faces: list[FaceDetection] = []
        for event in events:
            # Face identity is nested in metadata — we need the full event detail
            # The events list endpoint may include thumbnail metadata
            if "face" not in event.smart_types and "person" not in event.smart_types:
                continue

        # For face recognition, query with more detail
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback_minutes * 60 * 1000)
        data = await self._client.get(
            "/proxy/protect/api/events",
            params={
                "start": start_ms,
                "end": now_ms,
                "types": ["smartDetectZone", "smartDetectLine"],
            },
        )
        if data is None:
            return []

        for e in data if isinstance(data, list) else []:
            matched_name = self._extract_face_name(e)
            if not matched_name:
                continue

            faces.append(FaceDetection(
                person_name=matched_name,
                camera_name=self._resolve_camera_name(e.get("camera")),
                timestamp=e.get("start", 0),
                confidence=e.get("score", 0),
            ))

        if faces:
            unique_people = {f.person_name for f in faces}
            logger.debug("Face detections: %s", ", ".join(sorted(unique_people)))

        return faces

    async def get_person_detections(self, lookback_minutes: int = 30) -> list[DetectionEvent]:
        """Get anonymous person detection events (no face ID)."""
        events = await self.get_detection_events(lookback_minutes=lookback_minutes)
        return [e for e in events if "person" in e.smart_types]

    def match_zone(self, camera_name: str, zone: str) -> bool:
        """Check if a camera name matches a zone (using aliases)."""
        camera_lower = camera_name.lower()
        zone_lower = zone.lower()

        # Direct match
        if zone_lower in camera_lower:
            return True

        # Alias match
        aliases = self._zone_aliases.get(zone_lower, [])
        return any(alias.lower() in camera_lower for alias in aliases)

    @staticmethod
    def _extract_face_name(event: dict[str, Any]) -> str:
        """Extract a recognized face name from a Protect event.

        Face identity is stored in:
        metadata.detectedThumbnails[].group.matchedName
        """
        metadata = event.get("metadata", {})
        if not isinstance(metadata, dict):
            return ""

        thumbnails = metadata.get("detectedThumbnails", [])
        if not isinstance(thumbnails, list):
            return ""

        for thumb in thumbnails:
            if not isinstance(thumb, dict):
                continue
            # Only consider face thumbnails — skip vehicles/license plates
            if thumb.get("type") != "face":
                continue
            group = thumb.get("group", {})
            if not isinstance(group, dict):
                continue
            name = group.get("matchedName", "")
            if name and isinstance(name, str):
                return name

        return ""
