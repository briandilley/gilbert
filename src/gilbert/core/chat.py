"""Chat business logic — conversation access, summaries, and AI context.

These functions are used by both the AI service and the web layer. They
live in core so that core services do not depend on the web package.
"""

import re
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event, EventBusProvider

_GILBERT_MENTION = re.compile(r"\bgilbert\b", re.IGNORECASE)

# Pseudo-user id reserved for Gilbert (the AI) in @-mention markup.
# Gilbert isn't a row in the user table — assistant messages carry an
# empty ``author_id`` — but the mention picker surfaces him as a chip
# alongside humans so the syntax is symmetric. Treat this id as a
# "yes, the AI was addressed" hint, not a routable user_id.
GILBERT_MENTION_USER_ID = "gilbert"

# Markdown-link-shaped mention syntax stored in message content:
#     @[Display Name](user_id)
# The display name is purely cosmetic — the SPA shows whatever is in
# the brackets — and the user_id is the stable identifier the picker
# resolved to at insert time. A Marked.js extension on the SPA side
# parses this into a styled chip. Plain ``@gilbert`` (the legacy
# bare-name match) still triggers the AI but isn't a structured mention.
_MENTION_RE = re.compile(r"@\[([^\]\n]+)\]\(([A-Za-z0-9._:-]+)\)")

# Bare-name mention pattern for post-processing AI replies. The SPA's
# picker writes structured tags directly, but Gilbert composes plain
# prose like ``@Root`` — we rewrite those to ``@[Root](root)`` on the
# way out so the chip + notification machinery treats them the same
# as user-initiated mentions.
#
# ``(?<![\w@])`` rejects in-word ``@`` so an email address like
# ``alice@example.com`` doesn't accidentally trigger; also rejects
# ``@@Name`` and the second ``@`` of a typo. Capture group 2 is the
# bare display name; matched case-insensitively against room members.
_BARE_MENTION_RE = re.compile(r"(?<![\w@])@(\w[\w.\-]*)")


def check_conversation_access(
    data: dict[str, Any],
    user: UserContext,
    *,
    require_member: bool = False,
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
    # Allow invited users to see room info (but not require_member actions)
    if not require_member:
        invites = data.get("invites", [])
        if any(inv.get("user_id") == user.user_id for inv in invites):
            return None
    conv_owner = data.get("user_id", "")
    if conv_owner and conv_owner == user.user_id:
        return None
    if conv_owner or members:
        return "Access denied"
    return None


def extract_mentions(content: str) -> list[str]:
    """Return user ids @-mentioned in ``content``, in document order.

    Deduplicates while preserving order so the sidebar / notification
    layers see one entry per addressed user even if a message mentions
    them twice. The pseudo-id ``gilbert`` (see ``GILBERT_MENTION_USER_ID``)
    is included as-is — callers that don't care about AI mentions
    should filter it out.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _MENTION_RE.finditer(content or ""):
        uid = match.group(2)
        if uid and uid not in seen:
            seen.add(uid)
            result.append(uid)
    return result


def filter_mentions_to_members(
    content: str, member_user_ids: set[str]
) -> tuple[list[str], bool]:
    """Validate extracted mentions against a room's member list.

    Returns ``(valid_user_ids, mentions_gilbert_via_tag)``. Mentions
    that point at non-members are silently dropped — a user can't
    @-mention someone who isn't in the room. The Gilbert pseudo-id
    is always accepted (he's not in any room's member list but the
    SPA picker should still surface him).
    """
    valid: list[str] = []
    mentions_g = False
    for uid in extract_mentions(content):
        if uid == GILBERT_MENTION_USER_ID:
            mentions_g = True
            continue
        if uid in member_user_ids:
            valid.append(uid)
    return valid, mentions_g


def resolve_bare_mentions_to_structured(
    content: str,
    members: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Rewrite bare ``@Name`` to ``@[Name](user_id)`` for known members.

    The mention picker on the SPA writes structured tags directly,
    but the AI's reply text is free-form prose — it'll happily write
    ``@Root`` as plain words. This helper closes the loop: scan an
    AI reply for bare ``@Name`` tokens, match them case-insensitively
    against the room's members + ``Gilbert``, and replace each with
    the structured tag using the member's canonical display name.

    Returns ``(rewritten_content, list_of_resolved_user_ids)``.

    Already-structured tags (``@[..](..)``) are left alone — the
    regex doesn't cross over them because ``@[`` isn't matched by
    the bare-mention pattern. Names that don't resolve to a member
    pass through untouched as plain text — we deliberately don't
    invent users.
    """
    if not content or not members:
        # ``Gilbert`` is allowed even with an empty member list since
        # he's not a real member; fall through to the rewrite below.
        if not content:
            return content, []

    # Build a lookup: lowercased-name → canonical {user_id, display_name}.
    by_lower: dict[str, dict[str, str]] = {}
    for m in members:
        name = str(m.get("display_name") or "").strip()
        uid = str(m.get("user_id") or "").strip()
        if not name or not uid:
            continue
        by_lower[name.lower()] = {"user_id": uid, "display_name": name}
    # Always allow ``@Gilbert`` regardless of who's in the room.
    by_lower.setdefault(
        "gilbert", {"user_id": GILBERT_MENTION_USER_ID, "display_name": "Gilbert"}
    )

    resolved_ids: list[str] = []
    seen: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        raw_name = match.group(1)
        key = raw_name.lower()
        target = by_lower.get(key)
        if target is None:
            return match.group(0)  # no resolution — leave plain
        if target["user_id"] not in seen:
            seen.add(target["user_id"])
            resolved_ids.append(target["user_id"])
        return f"@[{target['display_name']}]({target['user_id']})"

    rewritten = _BARE_MENTION_RE.sub(_sub, content)
    return rewritten, resolved_ids


def conv_summary(
    c: dict[str, Any],
    *,
    shared: bool,
    viewer_user_id: str = "",
) -> dict[str, Any]:
    """Build a lightweight conversation summary for the sidebar.

    When ``viewer_user_id`` is provided the result includes
    ``unread_mentions_count`` — the number of messages newer than that
    viewer's ``last_read_mention_index`` cursor that mention them.
    Cursor lives on the member entry (set by
    ``chat.conversation.mark_mentions_read``). When the viewer isn't
    a member, or no cursor exists yet, the count starts from message
    index 0 — i.e. every unread mention counts.
    """
    messages = c.get("messages", [])
    preview = ""
    for m in messages:
        if m.get("role") == "user":
            preview = m.get("content", "")[:100]
            break
    title = c.get("title", "") or preview[:60] or "New conversation"
    metadata = c.get("metadata") or {}
    summary: dict[str, Any] = {
        "conversation_id": c.get("_id", ""),
        "title": title,
        "preview": preview,
        "updated_at": c.get("updated_at", ""),
        "message_count": len(messages),
        "shared": shared,
        # Optional grouping hint — ``"agent"`` for agent personal convs,
        # ``"war_room"`` for goal war rooms, ``""`` for everything else.
        # Populated by services that own conversation creation.
        "kind": str(metadata.get("kind") or ""),
        "agent_id": str(metadata.get("agent_id") or ""),
        # Parent link for subagent child conversations — empty string for
        # regular conversations. Lets the frontend sidebar nest them.
        "parent_conversation_id": str(c.get("parent_conversation_id") or ""),
    }
    if shared:
        members = c.get("members", [])
        summary["member_count"] = len(members)
        summary["members"] = [
            {"user_id": m["user_id"], "display_name": m.get("display_name", "")} for m in members
        ]
        summary["visibility"] = c.get("visibility", "public")
        summary["is_member"] = c.get("_is_member", True)
        summary["is_invited"] = c.get("_is_invited", False)

    # Per-viewer unread counts. Two cursors, two counts:
    #   - ``last_read_mention_index``  → ``unread_mentions_count``  (@-mentions only)
    #   - ``last_read_message_index``  → ``unread_messages_count``  (any new message)
    # Index-based cursors (not timestamps) because messages don't carry
    # per-row created_at fields today — the position in ``messages[]``
    # is the source of truth for "newer than what I've seen." Messages
    # the viewer wrote themselves never count as unread either way —
    # you don't get a badge for your own message.
    if viewer_user_id:
        mention_cursor = -1
        message_cursor = -1
        for m in c.get("members", []):
            if m.get("user_id") == viewer_user_id:
                raw_mention = m.get("last_read_mention_index")
                if isinstance(raw_mention, int):
                    mention_cursor = raw_mention
                raw_msg = m.get("last_read_message_index")
                if isinstance(raw_msg, int):
                    message_cursor = raw_msg
                break
        unread_mentions = 0
        unread_messages = 0
        for idx, msg in enumerate(messages):
            author_id = msg.get("author_id")
            if author_id == viewer_user_id:
                continue  # your own message — never unread
            if idx > mention_cursor:
                mentioned = msg.get("mentioned_user_ids") or []
                if viewer_user_id in mentioned:
                    unread_mentions += 1
            if idx > message_cursor:
                unread_messages += 1
        summary["unread_mentions_count"] = unread_mentions
        summary["unread_messages_count"] = unread_messages
    return summary


def mentions_gilbert(message: str) -> bool:
    """Check if a message addresses Gilbert.

    Triggers on either the legacy bare-name regex (``\\bgilbert\\b``) or
    the structured ``@[Gilbert](gilbert)`` mention syntax. Used by the
    AI-chat path in shared rooms to decide whether to actually invoke
    the AI on a message or just persist it — bare-name detection stays
    so users can keep typing ``gilbert, what about...`` naturally.
    """
    if _GILBERT_MENTION.search(message):
        return True
    return GILBERT_MENTION_USER_ID in extract_mentions(message)


_RE_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_RE_LIST_ITEM = re.compile(r"^\s*[-*+]\s+(.+)$", re.MULTILINE)
_RE_ORDERED_ITEM = re.compile(r"^\s*\d+\.\s+(.+)$", re.MULTILINE)
_RE_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(.+?)\1")
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_BLANK_RUN = re.compile(r"\n\s*\n+")


def strip_markdown_for_speech(text: str) -> str:
    """Strip markdown structure so the text reads naturally when spoken.

    Drops code blocks (un-speakable), drops link URLs (keeps anchor text),
    drops images, strips emphasis markers, periodizes headings and list
    items so TTS pauses between them, and collapses blank-line runs into
    a single sentence break. Regex-based — no markdown library dependency.

    Used by ``AIService._speak_response`` to prepare chat replies for TTS.
    """
    if not text:
        return ""
    out = _RE_FENCED_CODE.sub(" ", text)
    out = _RE_IMAGE.sub(" ", out)
    out = _RE_LINK.sub(r"\1", out)
    out = _RE_INLINE_CODE.sub(r"\1", out)
    out = _RE_HEADING.sub(r"\1. ", out)
    out = _RE_LIST_ITEM.sub(r"\1.", out)
    out = _RE_ORDERED_ITEM.sub(r"\1.", out)
    out = _RE_EMPHASIS.sub(r"\2", out)
    out = _RE_HTML_TAG.sub(" ", out)
    out = _RE_BLANK_RUN.sub(". ", out)
    # Collapse any residual runs of whitespace to single spaces.
    out = re.sub(r"[ \t]+", " ", out)
    # Trim and collapse stray ". ." into a single ".".
    out = re.sub(r"\.\s*\.+", ".", out)
    return out.strip()


def build_room_context(
    data: dict[str, Any],
    user: UserContext,
    template: str,
) -> str:
    """Render the configured room-context system prompt.

    The full prompt is owned by ``AIService`` as the
    ``room_context_prompt`` ``ConfigParam`` (``ai_prompt=True``);
    callers must read ``self._room_context_prompt`` and pass it in
    via ``template``. We only do the two runtime substitutions:

    - ``{room_title}`` — the shared room's title.
    - ``{members}``    — indented bullet list of members with role
      (owner / member) and a marker on the current speaker.

    ``str.replace`` instead of ``str.format`` so admin-edited
    templates with stray braces don't blow up — unknown placeholders
    pass through as literal text.
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

    return template.replace("{room_title}", title).replace("{members}", members_str)


async def publish_event(gilbert: Any, event_type: str, data: dict[str, Any]) -> None:
    """Publish an event to the event bus if available."""
    event_bus_svc = gilbert.service_manager.get_by_capability("event_bus")
    if event_bus_svc is None:
        return

    if isinstance(event_bus_svc, EventBusProvider):
        await event_bus_svc.bus.publish(
            Event(
                event_type=event_type,
                data=data,
                source="chat",
            )
        )
