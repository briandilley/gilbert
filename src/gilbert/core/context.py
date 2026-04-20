"""Request context — async-safe current-user and -conversation propagation."""

from contextvars import ContextVar

from gilbert.interfaces.auth import UserContext

_current_user: ContextVar[UserContext] = ContextVar("_current_user")
_current_conversation_id: ContextVar[str | None] = ContextVar(
    "_current_conversation_id", default=None
)


def get_current_user() -> UserContext:
    """Return the current user, or ``UserContext.SYSTEM`` if none is set."""
    return _current_user.get(UserContext.SYSTEM)


def set_current_user(user: UserContext) -> None:
    """Set the current user for the running async context."""
    _current_user.set(user)


def get_current_conversation_id() -> str | None:
    """Return the conversation id for the running async context, or None.

    Lives in a ContextVar rather than an instance attribute on the
    singleton AIService so two overlapping ``chat()`` calls (two users,
    two tabs, shared rooms) don't trample each other — each task reads
    its own conv id and events publish with the right routing key.
    """
    return _current_conversation_id.get()


def set_current_conversation_id(conversation_id: str | None) -> None:
    """Set the conversation id for the running async context.

    Should only be called at the start of a chat turn. Parallel tool
    tasks spawned via ``asyncio.Task(..., context=copy_context())``
    inherit the value set here, keeping per-turn conv id consistent
    even when the AIService singleton is mid-flight for another turn.
    """
    _current_conversation_id.set(conversation_id)
