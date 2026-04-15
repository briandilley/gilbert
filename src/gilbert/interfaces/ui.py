"""UI block definitions — structured UI elements that tools can push to the chat."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from gilbert.interfaces.attachments import FileAttachment


@dataclass(frozen=True)
class UIOption:
    """A single option for select, radio, checkbox, or button groups."""

    value: str
    label: str
    selected: bool = False


@dataclass(frozen=True)
class UIElement:
    """A single element in a UI form.

    The ``type`` field determines which other fields are relevant:

    - ``text``: single-line text input
    - ``textarea``: multi-line text input (uses ``rows``)
    - ``select``: dropdown (uses ``options``)
    - ``radio``: radio button group (uses ``options``)
    - ``checkbox``: single toggle or multi-select (uses ``options`` if multiple)
    - ``range``: slider (uses ``min_val``, ``max_val``, ``step``)
    - ``buttons``: row of buttons — clicking one submits the form with that value
    - ``label``: display-only text
    - ``image``: display-only image (uses ``url``, ``label`` as alt text, ``max_width``)
    - ``separator``: horizontal rule
    """

    type: str
    name: str = ""
    label: str = ""
    placeholder: str = ""
    default: Any = None
    required: bool = False
    options: list[UIOption] = field(default_factory=list)
    min_val: float | None = None
    max_val: float | None = None
    step: float | None = None
    rows: int = 4
    url: str = ""
    """Image URL for ``image`` elements. Ignored by other types."""
    max_width: int = 0
    """Maximum rendered width in pixels for ``image`` elements.
    ``0`` lets the renderer pick a default (currently 96 px for
    thumbnails)."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON transport to frontend."""
        d: dict[str, Any] = {"type": self.type}
        if self.name:
            d["name"] = self.name
        if self.label:
            d["label"] = self.label
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.default is not None:
            d["default"] = self.default
        if self.required:
            d["required"] = True
        if self.options:
            d["options"] = [
                {"value": o.value, "label": o.label, **({"selected": True} if o.selected else {})}
                for o in self.options
            ]
        if self.min_val is not None:
            d["min"] = self.min_val
        if self.max_val is not None:
            d["max"] = self.max_val
        if self.step is not None:
            d["step"] = self.step
        if self.type == "textarea":
            d["rows"] = self.rows
        if self.url:
            d["url"] = self.url
        if self.max_width:
            d["max_width"] = self.max_width
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UIElement:
        """Deserialize from stored/transported JSON."""
        options = [
            UIOption(value=o["value"], label=o["label"], selected=o.get("selected", False))
            for o in data.get("options", [])
        ]
        return cls(
            type=data.get("type", "label"),
            name=data.get("name", ""),
            label=data.get("label", ""),
            placeholder=data.get("placeholder", ""),
            default=data.get("default"),
            required=data.get("required", False),
            options=options,
            min_val=data.get("min"),
            max_val=data.get("max"),
            step=data.get("step"),
            rows=data.get("rows", 4),
            url=data.get("url", ""),
            max_width=data.get("max_width", 0),
        )


@dataclass(frozen=True)
class UIBlock:
    """A structured UI block that a tool pushes to the chat.

    Currently only the ``"form"`` block type is supported.
    """

    block_type: str = "form"
    block_id: str = ""
    title: str = ""
    elements: list[UIElement] = field(default_factory=list)
    submit_label: str = "Submit"
    tool_name: str = ""
    for_user: str = ""  # empty = visible to all members
    exclude_user: str = ""  # hide from this user

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON transport and persistence."""
        d: dict[str, Any] = {
            "block_type": self.block_type,
            "block_id": self.block_id or str(uuid.uuid4()),
            "title": self.title,
            "elements": [e.to_dict() for e in self.elements],
            "submit_label": self.submit_label,
            "tool_name": self.tool_name,
        }
        if self.for_user:
            d["for_user"] = self.for_user
        if self.exclude_user:
            d["exclude_user"] = self.exclude_user
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UIBlock:
        """Deserialize from stored/transported JSON."""
        elements = [UIElement.from_dict(e) for e in data.get("elements", [])]
        return cls(
            block_type=data.get("block_type", "form"),
            block_id=data.get("block_id", ""),
            title=data.get("title", ""),
            elements=elements,
            submit_label=data.get("submit_label", "Submit"),
            tool_name=data.get("tool_name", ""),
            for_user=data.get("for_user", ""),
            exclude_user=data.get("exclude_user", ""),
        )


@dataclass
class ToolOutput:
    """Extended return type for execute_tool().

    Tools can return either a plain ``str`` (backward compatible) or a
    ``ToolOutput`` instance. The AI service normalizes both: ``text``
    goes into the AI conversation as the tool result, ``ui_blocks`` are
    collected and delivered to the frontend, and ``attachments`` ride
    back on the final assistant ``Message`` so the user can download
    tool-produced files (PDFs, images, spreadsheets, …) straight from
    the chat bubble.
    """

    text: str
    ui_blocks: list[UIBlock] = field(default_factory=list)
    attachments: tuple[FileAttachment, ...] = ()
