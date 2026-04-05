"""System browser route — inspect services, config, and tools."""

from typing import Any

from fastapi import APIRouter, Request

from gilbert.core.app import Gilbert
from gilbert.core.services.configuration import ConfigurationService
from gilbert.interfaces.configuration import Configurable
from gilbert.interfaces.tools import ToolProvider
from gilbert.web import templates

router = APIRouter()


def _gather_services(gilbert: Gilbert) -> list[dict[str, Any]]:
    """Collect service info, config params, and tools for display."""
    sm = gilbert.service_manager
    config_svc = sm.get_by_capability("configuration")

    services: list[dict[str, Any]] = []
    all_registered = list(sm._registered.keys())

    for name in all_registered:
        svc = sm._registered[name]
        info = svc.service_info()
        started = name in sm.started_services
        failed = name in sm.failed_services

        entry: dict[str, Any] = {
            "name": info.name,
            "capabilities": sorted(info.capabilities),
            "requires": sorted(info.requires),
            "optional": sorted(info.optional),
            "started": started,
            "failed": failed,
            "config_params": [],
            "config_values": {},
            "tools": [],
        }

        # Config params
        if isinstance(svc, Configurable):
            entry["config_namespace"] = svc.config_namespace
            entry["config_params"] = [
                {
                    "key": p.key,
                    "type": p.type.value,
                    "description": p.description,
                    "default": p.default,
                    "restart_required": p.restart_required,
                }
                for p in svc.config_params()
            ]
            if isinstance(config_svc, ConfigurationService):
                entry["config_values"] = config_svc.get_section(svc.config_namespace)

        # Tools
        if isinstance(svc, ToolProvider):
            entry["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": [
                        {
                            "name": p.name,
                            "type": p.type.value,
                            "description": p.description,
                            "required": p.required,
                        }
                        for p in t.parameters
                    ],
                }
                for t in svc.get_tools()
            ]

        services.append(entry)

    return services


@router.get("/system")
async def system_browser(request: Request):  # type: ignore[no-untyped-def]
    gilbert: Gilbert = request.app.state.gilbert
    services = _gather_services(gilbert)
    return templates.TemplateResponse(
        "system.html", {"request": request, "services": services}
    )
