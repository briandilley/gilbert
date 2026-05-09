"""Shared UI block helpers — preview/confirm pattern for mutating tools.

Mutating AI tools that have meaningful blast radius (sending real email
invites, deleting calendar events, muting alerts, deleting health
metrics) follow a two-call preview/confirm protocol:

1. The AI calls the tool with ``confirm=False`` (the default). The
   service does NOT touch the backend; instead it returns a
   ``ToolOutput`` whose ``text`` is a short summary and whose
   ``ui_blocks`` contains a single ``UIBlock`` with a Confirm/Cancel
   ``buttons`` element.
2. The user clicks Confirm. The SPA submits the form via
   ``POST /chat/form-submit``; the AI re-invokes the same tool with the
   same arguments plus ``confirm=True``, and the service performs the
   actual mutation.

This module owns the helper that builds that preview ``ToolOutput`` so
every mutating tool produces a consistent UI: same button labels, same
layout, same ``tool_name`` round-trip behaviour. Future features
(``mute_camera_alerts``, health-record deletion, future mutating tools)
share this helper instead of re-implementing the form scaffold.

Calendar (`feature 01`) is the first caller; subsequent features import
``confirm_or_execute`` (or use ``build_confirm_block`` directly when
they need finer control over the block contents).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement, UIOption

CONFIRM_BUTTON_VALUE = "confirm"
"""The value carried by the Confirm button on every preview block. The
form submit ends up as a chat message that includes ``"action: confirm"``
along with the original arguments — the AI sees the user's intent and
re-invokes the tool with ``confirm=True``.
"""

CANCEL_BUTTON_VALUE = "cancel"
"""The value carried by the Cancel button. When the user clicks Cancel
the AI sees ``"action: cancel"`` and acknowledges without re-invoking
the tool."""


def build_confirm_block(
    *,
    tool_name: str,
    title: str,
    summary_lines: list[str],
    arguments: dict[str, Any],
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    submit_label: str = "Confirm",
) -> UIBlock:
    """Build a Confirm/Cancel UI block for a previewed mutation.

    The block carries:

    - A ``label`` element with markdown-friendly summary text (one line
      per entry in ``summary_lines``).
    - A hidden ``text`` element holding the original ``arguments`` as
      JSON, so the AI re-receives them when the form submits and can
      re-invoke the tool with the same payload plus ``confirm=True``.
    - A ``buttons`` element with two options whose values are
      ``CONFIRM_BUTTON_VALUE`` / ``CANCEL_BUTTON_VALUE``.

    Tools call this when ``confirm=False`` and wrap the result in a
    ``ToolOutput`` (or use :func:`confirm_or_execute` for the common
    branch-on-confirm idiom).
    """
    summary_text = "\n".join(line for line in summary_lines if line)
    elements: list[UIElement] = [
        UIElement(type="label", name="summary", label=summary_text),
        UIElement(
            type="text",
            name="pending_arguments",
            label="",
            default=json.dumps(arguments, default=str, sort_keys=True),
        ),
        UIElement(
            type="buttons",
            name="action",
            options=[
                UIOption(value=CONFIRM_BUTTON_VALUE, label=confirm_label),
                UIOption(value=CANCEL_BUTTON_VALUE, label=cancel_label),
            ],
        ),
    ]
    return UIBlock(
        title=title,
        elements=elements,
        submit_label=submit_label,
        tool_name=tool_name,
    )


def build_preview_output(
    *,
    tool_name: str,
    title: str,
    summary: str,
    summary_lines: list[str],
    arguments: dict[str, Any],
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> ToolOutput:
    """Return a ``ToolOutput`` containing the preview block.

    ``summary`` becomes the text the AI sees as the tool result —
    typically a short natural-language sentence ("I'm about to create
    'Team sync' on Tuesday at 3pm — confirm?"). ``summary_lines`` is
    the longer breakdown rendered inside the form (one bullet per
    field).
    """
    block = build_confirm_block(
        tool_name=tool_name,
        title=title,
        summary_lines=summary_lines,
        arguments=arguments,
        confirm_label=confirm_label,
        cancel_label=cancel_label,
        submit_label=confirm_label,
    )
    return ToolOutput(text=summary, ui_blocks=[block])


async def confirm_or_execute(
    *,
    confirm: bool,
    tool_name: str,
    title: str,
    summary: str,
    summary_lines: list[str],
    arguments: dict[str, Any],
    execute: Callable[[], Awaitable[str | ToolOutput]],
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> str | ToolOutput:
    """Preview / confirm gate for a mutating tool.

    When ``confirm`` is ``False`` (the default for mutating tools), the
    helper returns a preview ``ToolOutput`` and never calls ``execute``.
    When ``confirm`` is ``True`` it awaits ``execute`` and returns its
    result verbatim.

    Tools wrap their actual mutation logic in the ``execute`` thunk so
    the helper owns the branching:

    .. code-block:: python

        return await confirm_or_execute(
            confirm=bool(args.get("confirm")),
            tool_name="create_event",
            title="Create event",
            summary="Create 'Team sync' on Tuesday at 3pm — confirm?",
            summary_lines=["title: Team sync", "start: ...", "..."],
            arguments=args,
            execute=lambda: self._do_create_event(args),
        )

    Confirmed tools therefore have one place to apply their mutation,
    and the preview path can never accidentally fall through to a
    backend write.
    """
    if not confirm:
        return build_preview_output(
            tool_name=tool_name,
            title=title,
            summary=summary,
            summary_lines=summary_lines,
            arguments=arguments,
            confirm_label=confirm_label,
            cancel_label=cancel_label,
        )
    return await execute()


__all__ = [
    "CONFIRM_BUTTON_VALUE",
    "CANCEL_BUTTON_VALUE",
    "build_confirm_block",
    "build_preview_output",
    "confirm_or_execute",
]
