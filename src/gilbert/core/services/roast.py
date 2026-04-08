"""Roast service — occasionally roasts a random person detected in the building.

A fun service that runs hourly and, with configurable probability, picks
a random present person and delivers a playful roast via TTS/speakers.
"""

from __future__ import annotations

import logging
import random

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

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
        self._probability: float = 0.10
        self._ai_prompt: str = (
            "Generate a playful, friendly roast of {name}. "
            "Be funny and teasing but never mean or hurtful. "
            "Keep it to 1-2 sentences."
        )
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="roast",
            capabilities=frozenset({"roast"}),
            requires=frozenset({"scheduler"}),
            optional=frozenset({"ai_chat", "presence", "text_to_speech", "speaker_control"}),
            ai_calls=frozenset({"roast"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Load config
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("roast")
                self._probability = float(section.get("probability", 0.10))
                self._ai_prompt = section.get("ai_prompt", self._ai_prompt)

        # Register hourly job with the scheduler
        from gilbert.core.services.scheduler import SchedulerService
        from gilbert.interfaces.scheduler import Schedule

        scheduler = resolver.require_capability("scheduler")
        if isinstance(scheduler, SchedulerService):
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
            from gilbert.core.services.presence import PresenceService

            if isinstance(presence_svc, PresenceService):
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
        if self._resolver is not None:
            ai_svc = self._resolver.get_capability("ai_chat")
            if ai_svc is not None:
                try:
                    from gilbert.interfaces.auth import UserContext

                    prompt = self._ai_prompt.format(name=name)
                    response, _, _ui = await ai_svc.chat(
                        prompt,
                        user_ctx=UserContext.SYSTEM,
                        ai_call="roast",
                    )
                    if response and len(response) < 500:
                        return response.strip()
                except Exception:
                    logger.warning("AI roast generation failed, using template", exc_info=True)

        # Fallback to template
        template = random.choice(_DEFAULT_ROASTS)
        return template.format(name=name)

    async def _announce(self, text: str) -> None:
        """Announce roast via speakers if available."""
        if self._resolver is None:
            return

        speaker_svc = self._resolver.get_capability("speaker_control")
        if speaker_svc is None:
            logger.debug("No speaker service — roast not announced aloud: %s", text)
            return

        try:
            await speaker_svc.announce(text)
        except Exception:
            logger.warning("Failed to announce roast", exc_info=True)
