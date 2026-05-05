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
    "workspace.": 200,
    # user (100)
    "presence.": 100,
    "timer.": 100,
    "knowledge.": 100,
    # Inbox events are user-level — any user can have a shared mailbox.
    # The WS layer applies a per-event mailbox-access filter on top of
    # this, so a user only sees events for mailboxes they can access.
    "inbox.": 100,
    # Notifications are user-level events; the WS layer's
    # can_see_notification_event filter narrows delivery to the
    # specific recipient by matching event.data["user_id"].
    "notification.": 100,
    # auth.user.roles.changed fires on role mutation. The WS layer
    # restricts delivery to admins + the affected user themselves.
    "auth.": 100,
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
