"""Dashboard route — main landing page with tool cards."""

from fastapi import APIRouter, Request

from gilbert.web import templates

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):  # type: ignore[no-untyped-def]
    cards = [
        {
            "title": "System Browser",
            "description": "View services, capabilities, configuration, and tools.",
            "url": "/system",
            "icon": "&#9881;",  # gear
        },
        {
            "title": "Chat",
            "description": "Talk to Gilbert (coming soon).",
            "url": "#",
            "icon": "&#128172;",  # speech bubble
            "disabled": True,
        },
    ]
    return templates.TemplateResponse(request, "dashboard.html", {"cards": cards})
