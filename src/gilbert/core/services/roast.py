"""Roast service — occasionally roasts a random person detected in the building.

A fun service that runs hourly and, with configurable probability, picks
a random present person and delivers a playful roast via TTS/speakers.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_DEFAULT_ROASTS = [
    "Hey {name}, just checking — are you working or just standing there looking pretty?",
    "Quick update everyone — {name} has been staring at the same screen for 15 minutes.",
    "I'm not saying {name} is slow today, but I've seen coffee brew faster.",
    "Hey {name}, I just want you to know that the WiFi is working harder than you right now.",
    "{name}, you know you can actually type with both hands, right?",
    "Breaking news: {name} just discovered the copy-paste shortcut. Productivity up 400%.",
    "Hey {name}, just a reminder that the chair does NOT count as a team member.",
    "Someone tell {name} that watching YouTube tutorials counts as work. Just kidding.",
    "{name}, is that a new personal record for longest bathroom break?",
    "I've seen {name} check the fridge three times. Still nothing new in there.",
]


class RoastService(Service):
    """Occasionally roasts a random person detected in the building.

    Capabilities: roast
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._probability: float = 0.10
        self._ai_prompt: str = (
            "Generate a playful, friendly roast of {name}. "
            "Be funny and teasing but never mean or hurtful. "
            "Keep it to 1-2 sentences."
        )
        self._speakers: list[str] = []
        self._ai_profile: str = "standard"
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="roast",
            capabilities=frozenset({"roast"}),
            requires=frozenset({"scheduler"}),
            optional=frozenset({"ai_chat", "presence", "text_to_speech", "speaker_control"}),
            ai_calls=frozenset({"roast"}),
            toggleable=True,
            toggle_description="Random playful roasts",
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
                    logger.info("Roast service disabled")
                    return
                self._probability = float(section.get("probability", 0.10))
                self._speakers = section.get("speakers", [])
                self._ai_prompt = section.get("ai_prompt", self._ai_prompt)

        self._enabled = True

        # Register hourly job with the scheduler
        from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

        scheduler = resolver.require_capability("scheduler")
        if isinstance(scheduler, SchedulerProvider):
            scheduler.add_job(
                name="roast.hourly",
                schedule=Schedule.hourly_at(minute=0),
                callback=self._run_roast,
                system=True,
            )

        logger.info(
            "Roast service started (probability=%.0f%%)",
            self._probability * 100,
        )

    async def stop(self) -> None:
        pass

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "roast"

    @property
    def config_category(self) -> str:
        return "Communication"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="probability",
                type=ToolParameterType.NUMBER,
                description="Probability of roasting per hour (0.0-1.0).",
                default=0.10,
            ),
            ConfigParam(
                key="ai_prompt",
                type=ToolParameterType.STRING,
                description="AI prompt template for generating roasts. Use {name} as placeholder.",
                default="Generate a playful, friendly roast of {name}. Be funny and teasing but never mean or hurtful. Keep it to 1-2 sentences.",
                multiline=True,
            ),
            ConfigParam(
                key="speakers",
                type=ToolParameterType.ARRAY,
                description="Speaker names for roast announcements (empty = all).",
                default=[],
                choices_from="speakers",
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile for roast generation.",
                default="standard",
                choices_from="ai_profiles",
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._probability = float(config.get("probability", self._probability))
        self._ai_prompt = config.get("ai_prompt", self._ai_prompt)
        self._speakers = config.get("speakers", self._speakers)
        self._ai_profile = config.get("ai_profile", self._ai_profile)

    async def _run_roast(self) -> None:
        """Scheduler callback — roll the dice and maybe roast someone."""
        if random.random() > self._probability:
            logger.debug("Roast dice roll: no roast this time")
            return

        # Get present people
        name = await self._pick_random_person()
        if not name:
            logger.debug("No one present to roast")
            return

        # Generate roast
        roast_text = await self._generate_roast(name)
        logger.info("Roasting %s: %s", name, roast_text)

        # Announce via speakers if available
        await self._announce(roast_text)

    async def _pick_random_person(self) -> str:
        """Pick a random present person, or return empty string if none."""
        if self._resolver is None:
            return ""

        presence_svc = self._resolver.get_capability("presence")
        if presence_svc is None:
            return ""

        try:
            from gilbert.interfaces.presence import PresenceProvider

            if isinstance(presence_svc, PresenceProvider):
                present = await presence_svc.who_is_here()
                if not present:
                    return ""
                person = random.choice(present)
                from gilbert.core.user_utils import resolve_display_name

                return await resolve_display_name(person.user_id, self._resolver)
        except Exception:
            logger.warning("Failed to get present people", exc_info=True)

        return ""

    async def _generate_roast(self, name: str) -> str:
        """Generate a roast via AI if available, otherwise use a template."""
        from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
        from gilbert.interfaces.speaker import (
            SpeakerProvider,  # noqa: F401 — used by _announce via check
        )

        if self._resolver is not None:
            ai_svc = self._resolver.get_capability("ai_chat")
            if isinstance(ai_svc, AISamplingProvider):
                try:
                    prompt = self._ai_prompt.format(name=name)
                    # tools_override=[] forces a zero-tool call so the
                    # model can't call `announce` or any other tool as
                    # a side effect of generating roast text.
                    response = await ai_svc.complete_one_shot(
                        messages=[Message(role=MessageRole.USER, content=prompt)],
                        profile_name=self._ai_profile,
                        tools_override=[],
                    )
                    text = response.message.content.strip()
                    if text and len(text) < 500:
                        return text
                except Exception:
                    logger.warning("AI roast generation failed, using template", exc_info=True)

        # Fallback to template
        template = random.choice(_DEFAULT_ROASTS)
        return template.format(name=name)

    async def _announce(self, text: str) -> None:
        """Announce roast via speakers if available."""
        from gilbert.interfaces.speaker import SpeakerProvider

        if self._resolver is None:
            return

        speaker_svc = self._resolver.get_capability("speaker_control")
        if not isinstance(speaker_svc, SpeakerProvider):
            logger.debug("No speaker service — roast not announced aloud: %s", text)
            return

        try:
            await speaker_svc.announce(
                text,
                speaker_names=self._speakers or None,
                context="Playful roast / good-natured ribbing — deliver with bite",
            )
        except Exception:
            logger.warning("Failed to announce roast", exc_info=True)
