"""Shared chat helpers — used by both REST routes and WS RPC handlers."""

import re
from typing import Any

from gilbert.interfaces.auth import UserContext

_GILBERT_MENTION = re.compile(r'\bgilbert\b', re.IGNORECASE)


def check_conversation_access(
    data: dict[str, Any], user: UserContext, *, require_member: bool = False,
) -> str | None:
    """Check if user has access to a conversation.

    Returns None if access is granted, or an error message string if denied.
    """
    if user.user_id in ("system", "guest"):
        return None
    if data.get("shared") and data.get("visibility") == "public" and not require_member:
        return None
    members = data.get("members", [])
    if members:
        if any(m.get("user_id") == user.user_id for m in members):
            return None
    conv_owner = data.get("user_id", "")
    if conv_owner and conv_owner == user.user_id:
        return None
    if conv_owner or members:
        return "Access denied"
    return None


def conv_summary(c: dict[str, Any], *, shared: bool) -> dict[str, Any]:
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


def mentions_gilbert(message: str) -> bool:
    """Check if a message addresses Gilbert by name."""
    return bool(_GILBERT_MENTION.search(message))


def build_room_context(data: dict[str, Any], user: UserContext) -> str:
    """Build a system prompt for shared room conversations."""
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


async def publish_event(gilbert: Any, event_type: str, data: dict[str, Any]) -> None:
    """Publish an event to the event bus if available."""
    event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
    if event_bus_svc is None:
        return
    from gilbert.core.services.event_bus import EventBusService
    from gilbert.interfaces.events import Event

    if isinstance(event_bus_svc, EventBusService):
        await event_bus_svc.bus.publish(Event(
            event_type=event_type, data=data, source="chat",
        ))
