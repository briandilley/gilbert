"""Chat route — web-based AI conversation interface.

Supports both personal (single-user) and shared (multi-user) conversations.
Shared conversations have a ``members`` list and a ``shared: True`` flag.
"""

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from gilbert.core.app import Gilbert
from gilbert.interfaces.auth import UserContext
from gilbert.web.auth import require_authenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")


def _get_ai_service(request: Request) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    svc = gilbert.service_manager.get_by_capability("ai_chat")
    if svc is None:
        raise HTTPException(status_code=503, detail="AI service is not running")
    return svc


def _get_storage(request: Request) -> Any:
    gilbert: Gilbert = request.app.state.gilbert
    storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
    if storage_svc is None:
        raise HTTPException(status_code=503, detail="Storage not available")
    storage = getattr(storage_svc, "backend", None)
    if storage is None:
        raise HTTPException(status_code=503, detail="Storage not available")
    return storage


def _check_conversation_access(
    data: dict[str, Any], user: UserContext, *, require_member: bool = False,
) -> None:
    """Raise 403 if user has no access to this conversation.

    If ``require_member`` is True, public visibility alone is not enough —
    the user must be a member (used for write operations like sending messages).
    """
    if user.user_id in ("system", "guest"):
        return
    # Public shared rooms are readable by everyone
    if data.get("shared") and data.get("visibility") == "public" and not require_member:
        return
    # Shared conversation — check membership
    members = data.get("members", [])
    if members:
        if any(m.get("user_id") == user.user_id for m in members):
            return
    # Personal conversation — check ownership
    conv_owner = data.get("user_id", "")
    if conv_owner and conv_owner == user.user_id:
        return
    # No access
    if conv_owner or members:
        raise HTTPException(status_code=403, detail="Access denied")


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

    # Check access and build room context for shared conversations
    conv_data = None
    is_shared = False
    if conversation_id:
        storage = _get_storage(request)
        conv_data = await storage.get("ai_conversations", conversation_id)
        if conv_data is not None:
            _check_conversation_access(conv_data, user, require_member=True)
            is_shared = conv_data.get("shared", False)

    if is_shared:
        # Shared room: only invoke the AI if Gilbert is addressed by name.
        # Otherwise just store the message and broadcast it.
        addressed = _mentions_gilbert(message)
        tagged_message = f"[{user.display_name}]: {message}"

        # Always persist the user message to the conversation
        ai_svc_internal = ai_svc  # for _save helper
        response_text = ""
        ui_blocks: list[dict[str, Any]] = []

        if addressed:
            response_text, conv_id, ui_blocks = await ai_svc.chat(
                user_message=tagged_message,
                conversation_id=conversation_id,
                user_ctx=user,
                system_prompt=_build_room_context(conv_data, user),
                ai_call="human_chat",
            )
        else:
            # Store message without invoking AI
            conv_id = conversation_id
            from gilbert.interfaces.ai import Message, MessageRole
            messages = await ai_svc._load_conversation(conversation_id)
            messages.append(Message(
                role=MessageRole.USER, content=tagged_message,
                author_id=user.user_id, author_name=user.display_name,
            ))
            await ai_svc._save_conversation(conv_id, messages, user_ctx=user)

        # Broadcast to all members
        await _publish_event(request, "chat.message.created", {
            "conversation_id": conv_id,
            "author_id": user.user_id,
            "author_name": user.display_name,
            "content": response_text,
            "user_message": message,
            "ui_blocks": ui_blocks,
        })

        return {
            "response": response_text,
            "conversation_id": conv_id,
            "ui_blocks": ui_blocks,
        }

    # Personal chat — normal flow
    response_text, conv_id, ui_blocks = await ai_svc.chat(
        user_message=message,
        conversation_id=conversation_id,
        user_ctx=user,
        ai_call="human_chat",
    )

    return {
        "response": response_text,
        "conversation_id": conv_id,
        "ui_blocks": ui_blocks,
    }


@router.get("/conversations")
async def list_conversations(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> list[dict[str, Any]]:
    """List the current user's personal and shared conversations."""
    ai_svc = _get_ai_service(request)
    personal = await ai_svc.list_conversations(user_id=user.user_id, limit=30)
    shared = await ai_svc.list_shared_conversations(user_id=user.user_id, limit=30)

    results = []
    for c in shared:
        results.append(_conv_summary(c, shared=True))
    for c in personal:
        results.append(_conv_summary(c, shared=False))
    return results


def _conv_summary(c: dict[str, Any], *, shared: bool) -> dict[str, Any]:
    """Build a lightweight conversation summary for the sidebar."""
    messages = c.get("messages", [])
    preview = ""
    for m in messages:
        if m.get("role") == "user":
            preview = m.get("content", "")[:100]
            break
    title = c.get("title", "") or preview[:60] or "New conversation"
    summary: dict[str, Any] = {
        "conversation_id": c.get("_id", ""),
        "title": title,
        "preview": preview,
        "updated_at": c.get("updated_at", ""),
        "message_count": len(messages),
        "shared": shared,
    }
    if shared:
        members = c.get("members", [])
        summary["member_count"] = len(members)
        summary["members"] = [
            {"user_id": m["user_id"], "display_name": m.get("display_name", "")}
            for m in members
        ]
        summary["visibility"] = c.get("visibility", "public")
        summary["is_member"] = c.get("_is_member", True)
    return summary


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Load a conversation's messages."""
    storage = _get_storage(request)

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    _check_conversation_access(data, user)

    is_shared = data.get("shared", False)

    # Filter to user/assistant messages for display.
    display_messages = []
    for m in data.get("messages", []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        # Filter by visible_to
        visible_to = m.get("visible_to")
        if visible_to is not None and user.user_id not in visible_to:
            continue
        msg: dict[str, Any] = {"role": role, "content": m.get("content", "")}
        if is_shared:
            msg["author_id"] = m.get("author_id", "")
            msg["author_name"] = m.get("author_name", "")
        display_messages.append(msg)

    # Filter UI blocks by for_user
    ui_blocks = []
    for block in data.get("ui_blocks", []):
        for_user = block.get("for_user", "")
        if for_user and for_user != user.user_id:
            continue
        ui_blocks.append(block)

    result: dict[str, Any] = {
        "conversation_id": conversation_id,
        "title": data.get("title", ""),
        "messages": display_messages,
        "ui_blocks": ui_blocks,
        "updated_at": data.get("updated_at", ""),
        "shared": is_shared,
    }
    if is_shared:
        result["members"] = data.get("members", [])
        result["owner_id"] = data.get("user_id", "")
    return result


@router.post("/conversations/{conversation_id}/rename")
async def rename_conversation(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Rename a conversation."""
    storage = _get_storage(request)

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    _check_conversation_access(data, user)

    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    data["title"] = title
    await storage.put("ai_conversations", conversation_id, data)

    await _publish_event(request, "chat.conversation.renamed", {
        "conversation_id": conversation_id, "title": title,
    })

    return {"status": "ok", "title": title}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Delete a personal conversation."""
    storage = _get_storage(request)

    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Only the owner can delete; shared rooms use a different mechanism
    if data.get("shared"):
        raise HTTPException(status_code=400, detail="Use room destroy for shared conversations")
    conv_owner = data.get("user_id", "")
    if conv_owner and user.user_id not in ("system",) and conv_owner != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    await storage.delete("ai_conversations", conversation_id)

    return {"status": "ok"}


@router.post("/form-submit")
async def form_submit(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Submit a form that was rendered in chat.

    Expects JSON: ``{"conversation_id": "...", "block_id": "...", "values": {...}}``.
    """
    ai_svc = _get_ai_service(request)
    body = await request.json()

    conversation_id = body.get("conversation_id")
    block_id = body.get("block_id")
    values = body.get("values", {})

    if not conversation_id or not block_id:
        raise HTTPException(status_code=400, detail="conversation_id and block_id required")

    storage = _get_storage(request)

    block_title = "Form"
    conv_data = await storage.get("ai_conversations", conversation_id)
    if conv_data:
        _check_conversation_access(conv_data, user)
        for block in conv_data.get("ui_blocks", []):
            if block.get("block_id") == block_id:
                block["submitted"] = True
                block["submission"] = values
                block_title = block.get("title") or "Form"
                break
        await storage.put("ai_conversations", conversation_id, conv_data)

    # Build a human-readable summary for the AI
    form_message = f"[Form submitted: {block_title}]\n"
    for k, v in values.items():
        form_message += f"- {k}: {v}\n"

    response_text, conv_id, ui_blocks = await ai_svc.chat(
        user_message=form_message,
        conversation_id=conversation_id,
        user_ctx=user,
        ai_call="human_chat",
    )

    # Broadcast for shared conversations
    if conv_data and conv_data.get("shared"):
        await _publish_event(request, "chat.message.created", {
            "conversation_id": conv_id,
            "author_id": user.user_id,
            "author_name": user.display_name,
            "content": response_text,
            "user_message": form_message,
            "ui_blocks": ui_blocks,
        })

    return {
        "response": response_text,
        "conversation_id": conv_id,
        "ui_blocks": ui_blocks,
    }


# ---- Shared conversation management ----


@router.post("/shared")
async def create_shared_conversation(
    request: Request,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Create a shared chat room.

    Expects JSON: ``{"title": "...", "visibility": "public" | "invite"}``.
    Public rooms are visible to all users. Invite-only rooms only appear
    for members. The creator is added as owner.
    """
    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    visibility = body.get("visibility", "public")
    if visibility not in ("public", "invite"):
        raise HTTPException(status_code=400, detail="visibility must be 'public' or 'invite'")

    storage = _get_storage(request)
    conv_id = uuid.uuid4().hex

    members = [
        {"user_id": user.user_id, "display_name": user.display_name, "role": "owner"},
    ]

    data: dict[str, Any] = {
        "user_id": user.user_id,
        "title": title,
        "shared": True,
        "visibility": visibility,
        "members": members,
        "messages": [],
        "ui_blocks": [],
        "updated_at": datetime.now(UTC).isoformat(),
    }

    await storage.put("ai_conversations", conv_id, data)

    await _publish_event(request, "chat.conversation.created", {
        "conversation_id": conv_id,
        "title": title,
        "shared": True,
        "visibility": visibility,
        "members": members,
    })

    return {"conversation_id": conv_id, "title": title, "visibility": visibility, "members": members}


@router.post("/shared/{conversation_id}/join")
async def join_shared(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Join a public shared conversation."""
    storage = _get_storage(request)
    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not data.get("shared"):
        raise HTTPException(status_code=400, detail="Not a shared conversation")
    if data.get("visibility") != "public":
        raise HTTPException(status_code=403, detail="This room is invite-only")

    members: list[dict[str, Any]] = data.get("members", [])
    if any(m["user_id"] == user.user_id for m in members):
        return {"status": "already_member"}

    members.append({
        "user_id": user.user_id,
        "display_name": user.display_name,
        "role": "member",
    })
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)

    await _publish_event(request, "chat.member.joined", {
        "conversation_id": conversation_id,
        "user_id": user.user_id,
        "display_name": user.display_name,
    })

    return {"status": "ok", "members": members}


@router.post("/shared/{conversation_id}/invite")
async def invite_to_shared(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Invite a user to a shared conversation. Owner only.

    Expects JSON: ``{"user_id": "...", "display_name": "..."}``.
    """
    storage = _get_storage(request)
    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not data.get("shared"):
        raise HTTPException(status_code=400, detail="Not a shared conversation")
    if data.get("user_id") != user.user_id:
        raise HTTPException(status_code=403, detail="Only the room owner can invite")

    body = await request.json()
    target_id = body.get("user_id", "").strip()
    if not target_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    members: list[dict[str, Any]] = data.get("members", [])
    if any(m["user_id"] == target_id for m in members):
        return {"status": "already_member"}

    display_name = body.get("display_name", target_id)
    members.append({
        "user_id": target_id,
        "display_name": display_name,
        "role": "member",
    })
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)

    await _publish_event(request, "chat.member.joined", {
        "conversation_id": conversation_id,
        "user_id": target_id,
        "display_name": display_name,
    })

    # Notify the AI about the new member
    ai_svc = _get_ai_service(request)
    await ai_svc.chat(
        user_message=f"[{display_name} has joined the room]",
        conversation_id=conversation_id,
        ai_call="human_chat",
    )

    return {"status": "ok", "members": members}


@router.post("/shared/{conversation_id}/kick")
async def kick_from_shared(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Kick a user from a shared conversation. Owner only.

    Expects JSON: ``{"user_id": "..."}``.
    """
    storage = _get_storage(request)
    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not data.get("shared"):
        raise HTTPException(status_code=400, detail="Not a shared conversation")
    if data.get("user_id") != user.user_id:
        raise HTTPException(status_code=403, detail="Only the room owner can kick")

    body = await request.json()
    target_id = body.get("user_id", "").strip()
    if not target_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    if target_id == user.user_id:
        raise HTTPException(status_code=400, detail="Cannot kick yourself — use leave")

    members: list[dict[str, Any]] = data.get("members", [])
    kicked_name = ""
    for m in members:
        if m["user_id"] == target_id:
            kicked_name = m.get("display_name", target_id)
            break
    members = [m for m in members if m["user_id"] != target_id]
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)

    await _publish_event(request, "chat.member.kicked", {
        "conversation_id": conversation_id,
        "user_id": target_id,
        "display_name": kicked_name,
    })

    # Notify the AI
    ai_svc = _get_ai_service(request)
    await ai_svc.chat(
        user_message=f"[{kicked_name} has been removed from the room]",
        conversation_id=conversation_id,
        ai_call="human_chat",
    )

    return {"status": "ok", "members": members}


@router.post("/shared/{conversation_id}/leave")
async def leave_shared(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Leave a shared conversation.

    If the owner leaves, the room is destroyed and all members are kicked.
    If a regular member leaves, they are simply removed.
    """
    storage = _get_storage(request)
    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not data.get("shared"):
        raise HTTPException(status_code=400, detail="Not a shared conversation")

    is_owner = data.get("user_id") == user.user_id

    if is_owner:
        # Owner leaving = destroy room
        await storage.delete("ai_conversations", conversation_id)
        await _publish_event(request, "chat.conversation.destroyed", {
            "conversation_id": conversation_id,
            "reason": "owner_left",
        })
        return {"status": "destroyed"}

    # Regular member leaving
    members: list[dict[str, Any]] = data.get("members", [])
    leaving_name = ""
    for m in members:
        if m["user_id"] == user.user_id:
            leaving_name = m.get("display_name", user.user_id)
            break
    members = [m for m in members if m["user_id"] != user.user_id]
    data["members"] = members
    await storage.put("ai_conversations", conversation_id, data)

    await _publish_event(request, "chat.member.left", {
        "conversation_id": conversation_id,
        "user_id": user.user_id,
        "display_name": leaving_name,
    })

    # Notify the AI
    ai_svc = _get_ai_service(request)
    await ai_svc.chat(
        user_message=f"[{leaving_name} has left the room]",
        conversation_id=conversation_id,
        ai_call="human_chat",
    )

    return {"status": "ok", "members": members}


@router.get("/shared/{conversation_id}/members")
async def get_shared_members(
    request: Request,
    conversation_id: str,
    user: UserContext = Depends(require_authenticated),  # noqa: B008
) -> list[dict[str, Any]]:
    """List members of a shared conversation."""
    storage = _get_storage(request)
    data = await storage.get("ai_conversations", conversation_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    _check_conversation_access(data, user)
    return data.get("members", [])


# ---- Helpers ----


_GILBERT_MENTION = re.compile(r'\bgilbert\b', re.IGNORECASE)


def _mentions_gilbert(message: str) -> bool:
    """Check if a message addresses Gilbert by name."""
    return bool(_GILBERT_MENTION.search(message))


def _build_room_context(data: dict[str, Any], user: UserContext) -> str:
    """Build a system prompt for shared room conversations.

    Tells the AI about the room, its members, and instructs it to stay
    quiet unless directly addressed or acting through tools.
    """
    title = data.get("title", "Shared Room")
    members = data.get("members", [])
    owner_id = data.get("user_id", "")

    member_lines = []
    for m in members:
        role = "owner" if m["user_id"] == owner_id else "member"
        marker = " (you are speaking with them now)" if m["user_id"] == user.user_id else ""
        member_lines.append(f"  - {m.get('display_name', m['user_id'])} ({role}){marker}")

    members_str = "\n".join(member_lines) if member_lines else "  (no members)"

    return (
        f"You are Gilbert, an AI assistant in a shared chat room called \"{title}\".\n"
        f"Multiple users are in this room. Messages from users are prefixed with their name "
        f"in brackets, e.g. [Alice]: hello.\n\n"
        f"Current members:\n{members_str}\n\n"
        f"IMPORTANT: Stay quiet unless:\n"
        f"- Someone addresses you directly (mentions Gilbert, asks you something, etc.)\n"
        f"- A tool or service is making you interact with the room\n"
        f"- You are responding to a tool call\n"
        f"If no one is talking to you, respond with just an empty string.\n"
        f"When you do respond, be concise and helpful."
    )


async def _publish_event(
    request: Request, event_type: str, data: dict[str, Any],
) -> None:
    """Publish an event to the event bus if available."""
    gilbert: Gilbert = request.app.state.gilbert
    event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
    if event_bus_svc is None:
        return
    from gilbert.core.services.event_bus import EventBusService
    from gilbert.interfaces.events import Event

    if isinstance(event_bus_svc, EventBusService):
        await event_bus_svc.bus.publish(Event(
            event_type=event_type, data=data, source="chat",
        ))
