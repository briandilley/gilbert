"""Chat route — web-based AI conversation interface."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.web import templates
from gilbert.web.auth import require_authenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")


def _get_ai_service(request: Request) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    svc = gilbert.service_manager.get_by_capability("ai_chat")
    if svc is None:
        raise HTTPException(status_code=503, detail="AI service is not running")
    return svc


@router.get("")
async def chat_page(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> Any:
    """Render the chat interface."""
    gilbert: Gilbert = request.app.state.gilbert
    ai_svc = gilbert.service_manager.get_by_capability("ai_chat")
    ai_available = ai_svc is not None

    # Load user's recent conversations for the sidebar.
    conversations: list[dict[str, Any]] = []
    if ai_available:
        conversations = await ai_svc.list_conversations(user_id=user.user_id, limit=30)

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "ai_available": ai_available,
            "conversations": conversations,
            "user": user,
        },
    )


@router.post("/send")
async def chat_send(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Send a message and get the AI response.

    Expects JSON: ``{"message": "...", "conversation_id": "..." | null}``.
    Returns: ``{"response": "...", "conversation_id": "..."}``.
    """
    ai_svc = _get_ai_service(request)
    body = await request.json()

    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    conversation_id = body.get("conversation_id") or None

    response_text, conv_id = await ai_svc.chat(
        user_message=message,
        conversation_id=conversation_id,
        user_ctx=user,
        apply_chat_visibility=True,
    )

    return {
        "response": response_text,
        "conversation_id": conv_id,
    }


@router.get("/conversations")
async def list_conversations(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> list[dict[str, Any]]:
    """List the current user's conversations."""
    ai_svc = _get_ai_service(request)
    convos = await ai_svc.list_conversations(user_id=user.user_id, limit=30)
    # Return lightweight summaries.
    results = []
    for c in convos:
        messages = c.get("messages", [])
        # First user message as preview.
        preview = ""
        for m in messages:
            if m.get("role") == "user":
                preview = m.get("content", "")[:100]
                break
        title = c.get("title", "") or preview[:60] or "New conversation"
        results.append({
            "conversation_id": c["_id"],
            "title": title,
            "preview": preview,
            "updated_at": c.get("updated_at", ""),
            "message_count": len(messages),
        })
    return results


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Load a conversation's messages."""
    ai_svc = _get_ai_service(request)
    gilbert: Gilbert = request.app.state.gilbert
    storage_svc = gilbert.service_manager.get_by_capability("entity_storage")

    if storage_svc is None:
        raise HTTPException(status_code=503, detail="Storage not available")

    storage = getattr(storage_svc, "backend", None)
    if storage is None:
        raise HTTPException(status_code=503, detail="Storage not available")

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Ensure user owns this conversation (skip for guest users).
    conv_owner = data.get("user_id", "")
    if conv_owner and user.user_id not in ("system", "guest") and conv_owner != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Filter to user/assistant messages for display.
    display_messages = []
    for m in data.get("messages", []):
        role = m.get("role")
        if role in ("user", "assistant"):
            display_messages.append({
                "role": role,
                "content": m.get("content", ""),
            })

    return {
        "conversation_id": conversation_id,
        "title": data.get("title", ""),
        "messages": display_messages,
        "updated_at": data.get("updated_at", ""),
    }


@router.post("/conversations/{conversation_id}/rename")
async def rename_conversation(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Rename a conversation."""
    gilbert: Gilbert = request.app.state.gilbert
    storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
    if storage_svc is None:
        raise HTTPException(status_code=503, detail="Storage not available")

    storage = getattr(storage_svc, "backend", None)
    if storage is None:
        raise HTTPException(status_code=503, detail="Storage not available")

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    conv_owner = data.get("user_id", "")
    if conv_owner and user.user_id not in ("system", "guest") and conv_owner != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    data["title"] = title
    await storage.put("ai_conversations", conversation_id, data)

    # Emit event for WebSocket clients
    event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
    if event_bus_svc is not None:
        from gilbert.core.services.event_bus import EventBusService
        from gilbert.interfaces.events import Event

        if isinstance(event_bus_svc, EventBusService):
            await event_bus_svc.bus.publish(Event(
                event_type="chat.conversation.renamed",
                data={"conversation_id": conversation_id, "title": title},
                source="chat",
            ))

    return {"status": "ok", "title": title}
