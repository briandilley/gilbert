"""ACL policy defaults — event visibility, RPC permissions, and role levels.

Shared by both the core access-control service and the web layer so that
neither depends on the other for these constants.

Reserved keys on ``Event.data``
--------------------------------

``required_role`` — when an ``Event``'s ``data["required_role"]`` is one
of the canonical role names (``"admin"`` / ``"user"`` / ``"everyone"``),
``resolve_event_visibility`` returns the matching numeric level for that
role and the per-event-type prefix table is bypassed. Services use this
to override the *default* visibility for events whose audience is
narrower than the prefix would suggest (e.g. an admin-gated camera
publishing an event under ``camera.event.detected`` — which is
prefix-everyone — but with ``data["required_role"]="admin"`` so the WS
filter only delivers it to admin connections). Unknown / missing values
fall back to the prefix-based resolution.
"""

from collections.abc import Mapping
from typing import Any

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
    "workspace.": 200,
    # Camera events default to everyone; the camera service overrides on
    # a per-camera basis by writing ``data["required_role"]`` per the
    # ``resolve_event_visibility`` primitive below.
    "camera.": 200,
    # Backend-status events (connect/disconnect) are admin-only
    # diagnostics, distinct from the per-camera detection stream above.
    "camera.backend.": 0,
    # user (100)
    "presence.": 100,
    "timer.": 100,
    "knowledge.": 100,
    # Inbox events are user-level — any user can have a shared mailbox.
    # The WS layer applies a per-event mailbox-access filter on top of
    # this, so a user only sees events for mailboxes they can access.
    "inbox.": 100,
    # Calendar events are user-level — any user can have a shared
    # calendar account. Like inbox, the WS layer applies a per-event
    # account-access filter on top of this prefix-level gate.
    "calendar.": 100,
    # Feeds events are user-level — any user can have a shared feed
    # subscription. The WS layer applies a per-event feed-access
    # filter on top of this prefix-level gate, and
    # ``feed.briefing.ready`` is restricted to the recipient
    # ``user_id`` only (analogous to notification fan-out).
    "feed.": 100,
    # Tasks events are user-level. Handlers enforce per-list access on
    # mutations and queries; event consumers should treat list/task IDs
    # as hints and fetch current visible state through tasks RPCs.
    "tasks.": 100,
    "task.": 100,
    # Notifications are user-level events; the WS layer's
    # can_see_notification_event filter narrows delivery to the
    # specific recipient by matching event.data["user_id"].
    "notification.": 100,
    # Browser-speaker playback frames are user-level events; the WS
    # layer's can_see_speaker_browser_event filter narrows delivery
    # to the specific recipient by matching event.data["user_id"].
    "speaker.browser.": 100,
    # Read-aloud preference changes are user-level events; the WS
    # layer's can_see_chat_read_aloud_event filter narrows delivery
    # to the specific recipient by matching event.data["user_id"].
    "chat.read_aloud.": 100,
    # auth.user.roles.changed fires on role mutation. The WS layer
    # restricts delivery to admins + the affected user themselves.
    "auth.": 100,
    # Health events are user-level — owner-only filtering happens via
    # the per-event ``can_see_health_event`` filter in
    # ``web/ws_protocol.py`` (mirrors the notification pattern).
    "health.": 100,
    # admin (0)
    "service.": 0,
    "config.": 0,
    "acl.": 0,
    # Self-improvement proposals — autonomously generated, admin triage.
    "proposal.": 0,
    # Conversation archive event carries the full message transcript
    # for last-chance observation extraction; restrict it to admins so
    # the transcript doesn't leak via the WS event stream.
    "chat.conversation.archiving": 0,
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
    "chat.message.cancel": 200,
    "chat.form.submit": 200,
    "chat.user.list": 200,
    # Slash-command discovery — response is already RBAC-filtered per
    # caller, so the listing endpoint itself is open to everyone.
    "slash.commands.list": 200,
    "dashboard.get": 200,
    "documents.": 200,
    "screens.list": 200,
    "skills.list": 200,
    "skills.conversation.": 200,
    "skills.workspace.": 200,
    "workspace.": 200,
    # user (100)
    "chat.": 100,
    # Greeting RPCs are user-level (settings UI can enumerate discovered
    # context providers and their enabled state).
    "greeting.": 100,
    # Scheduler: listing is user-level; state-changing operations on
    # system jobs require admin. Handlers enforce ownership checks on
    # user jobs so a non-admin user can only touch their own.
    "scheduler.job.list": 100,
    "scheduler.job.get": 100,
    "scheduler.job.remove": 100,
    "scheduler.job.enable": 0,
    "scheduler.job.disable": 0,
    "scheduler.job.run_now": 0,
    # Inbox RPCs are user-level; handlers enforce per-mailbox access
    # via can_access_mailbox / can_admin_mailbox on top of the level.
    "inbox.": 100,
    # Calendar RPCs are user-level; handlers enforce per-account
    # access via can_access_account / can_admin_account on top of the
    # prefix-level gate (any authenticated user may issue calendar.*
    # frames; per-account authorization is per-handler).
    "calendar.": 100,
    # Feeds RPCs are user-level; handlers enforce per-feed access
    # via can_access_feed / can_admin_feed on top of the prefix-level
    # gate (any authenticated user may issue feeds.* frames; per-feed
    # authorization is per-handler).
    "feeds.": 100,
    # Tasks RPCs are user-level; handlers enforce per-list access via
    # can_access_list / can_admin_list on top of the prefix-level gate.
    "tasks.": 100,
    # Camera RPCs are user-level; handlers enforce per-camera role
    # filtering on top of the prefix-level gate.
    "cameras.": 100,
    # Notifications are user-level; handlers enforce per-user ownership
    # so a user only ever sees / mutates their own notifications.
    "notification.": 100,
    # Browser plugin: credentials and VNC sessions are scoped per user;
    # handlers enforce ownership so a user can only see / mutate their
    # own.
    "browser.": 100,
    # Plugin UI extensions: any authenticated user can ask which panels
    # / routes the loaded plugins contribute. The handlers filter
    # per-entry by required_role.
    "ui.panels.": 100,
    "ui.routes.": 100,
    # Prompt contribution discovery — admin-only since the prompts
    # themselves live in admin Settings.
    "prompts.contributions.": 0,
    # MCP client: list/get/start/stop/test are user-level (handlers enforce
    # per-record visibility + ownership). Creating/updating ``shared`` or
    # ``public`` servers, or changing any record's scope/allowed_roles/
    # allowed_users, is admin-only — the handler layer upgrades the check
    # based on the payload, since the frame type alone can't express it.
    "mcp.servers.": 100,
    # MCP browser bridge: any authenticated user can announce their
    # own browser-hosted MCP servers — these are session-ephemeral,
    # private to the caller, and never touch shared state.
    "mcp.bridge.": 100,
    # Agent RPCs: any authenticated user can manage their own agents.
    # Handlers enforce per-agent ownership via load_agent_for_caller.
    "agents.": 100,
    # Goal RPCs: any authenticated user can manage their own goals.
    # Handlers enforce per-goal ownership (caller must own the goal,
    # i.e., goal.owner_user_id == caller.user_id) or be admin.
    "goals.": 100,
    # Deliverable RPCs (Phase 5): user-level. Handlers enforce per-goal
    # ownership through ``_load_goal_for_caller``. ``goals.dependencies.*``
    # is covered by the broader ``goals.`` prefix above.
    "deliverables.": 100,
    # MCP server (Gilbert-as-MCP): managing client registrations is
    # admin-only because creating a client grants an external
    # process permission to impersonate a Gilbert user's identity.
    "mcp.clients.": 0,
    # admin (0)
    "config.": 0,
    "roles.": 0,
    "system.": 0,
    "entities.": 0,
    "plugins.": 0,
    "gilbert.peer.publish": 0,
    "usage.": 0,
    # Self-improvement proposals — admin-only browse + triage.
    "proposals.": 0,
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


def resolve_event_visibility(
    event_type: str,
    data: Mapping[str, Any] | None = None,
) -> int:
    """Resolve the minimum role level for an event, honoring per-event overrides.

    When the event's ``data["required_role"]`` is one of the canonical
    role names (``"admin"`` / ``"user"`` / ``"everyone"``), the matching
    numeric level wins. Unknown / missing values fall back to the
    longest-prefix lookup in :data:`DEFAULT_EVENT_VISIBILITY`.

    See the module docstring for the contract on ``data["required_role"]``.
    """
    if data is not None:
        required = data.get("required_role")
        if isinstance(required, str):
            level = BUILTIN_ROLE_LEVELS.get(required)
            if level is not None:
                return level
    return resolve_default_event_level(event_type)
