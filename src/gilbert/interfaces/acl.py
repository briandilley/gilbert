"""ACL policy defaults — event visibility, RPC permissions, and role levels.

Shared by both the core access-control service and the web layer so that
neither depends on the other for these constants.
"""

# ── Built-in role levels ──────────────────────────────────────────────
# Canonical mapping of role names → numeric privilege levels.
# Lower number = more privileged. Used as a fallback when the full
# AccessControlService is not available.

BUILTIN_ROLE_LEVELS: dict[str, int] = {
    "admin": 0,
    "user": 100,
    "everyone": 200,
}

# ── Event visibility defaults ────────────────────────────────────────
# Maps event_type prefix → minimum role level required.
# Longest prefix match wins. System user (level -1) bypasses all.

DEFAULT_EVENT_VISIBILITY: dict[str, int] = {
    # everyone (200)
    "doorbell.": 200,
    "greeting.": 200,
    "alarm.": 200,
    "screen.": 200,
    "chat.": 200,
    "radio_dj.": 200,
    # user (100)
    "presence.": 100,
    "timer.": 100,
    "knowledge.": 100,
    # admin (0)
    "inbox.": 0,
    "service.": 0,
    "config.": 0,
    "acl.": 0,
}
DEFAULT_VISIBILITY_LEVEL: int = 100  # unlisted events → user role

# ── RPC handler permission defaults ──────────────────────────────────
# Maps frame type prefix → minimum role level required to call the handler.
# Same resolution logic as event visibility: longest prefix match wins.

DEFAULT_RPC_PERMISSIONS: dict[str, int] = {
    # everyone (200)
    "gilbert.ping": 200,
    "gilbert.sub.": 200,
    "chat.conversation.list": 200,
    "chat.conversation.create": 200,
    "chat.history.load": 200,
    "chat.message.send": 200,
    "chat.form.submit": 200,
    "chat.user.list": 200,
    "dashboard.get": 200,
    "documents.": 200,
    "screens.list": 200,
    "skills.list": 200,
    "skills.conversation.": 200,
    "skills.workspace.": 200,
    # user (100)
    "chat.": 100,
    # admin (0)
    "config.": 0,
    "roles.": 0,
    "inbox.": 0,
    "system.": 0,
    "entities.": 0,
    "gilbert.peer.publish": 0,
}
DEFAULT_RPC_LEVEL: int = 100  # unlisted frame types → user role


# ── Helpers ──────────────────────────────────────────────────────────

def resolve_default_rpc_level(frame_type: str) -> int:
    """Resolve the minimum role level from the hardcoded RPC defaults.

    Longest prefix match wins.
    """
    best_match = ""
    best_level = DEFAULT_RPC_LEVEL
    for prefix, level in DEFAULT_RPC_PERMISSIONS.items():
        if frame_type.startswith(prefix) and len(prefix) > len(best_match):
            best_match = prefix
            best_level = level
    return best_level


def resolve_default_event_level(event_type: str) -> int:
    """Resolve the minimum role level from the hardcoded event visibility defaults.

    Longest prefix match wins.
    """
    best_match = ""
    best_level = DEFAULT_VISIBILITY_LEVEL
    for prefix, level in DEFAULT_EVENT_VISIBILITY.items():
        if event_type.startswith(prefix) and len(prefix) > len(best_match):
            best_match = prefix
            best_level = level
    return best_level
