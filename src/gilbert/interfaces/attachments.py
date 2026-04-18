"""Provider-neutral file-attachment dataclass.

This type lives here (and not under ``interfaces/ai.py`` or
``interfaces/tools.py``) because it's referenced by both the message-level
dataclasses in ``ai.py`` and the tool-result dataclasses in ``tools.py``.
Hoisting it out breaks the import cycle those two modules would otherwise
form, and keeps ``attachments.py`` as the single source of truth.

``FileAttachment`` is used in two modes:

1. **Inline mode** — ``data`` or ``text`` carries the full content. This is
   how user uploads arrive from the chat input: the frontend base64-encodes
   the file, the server decodes and validates it, and the payload rides
   through the conversation row as part of the message. Images, PDFs, and
   xlsx uploads all use this mode.

2. **Workspace-reference mode** — ``workspace_skill`` and ``workspace_path``
   together name a file that already lives on disk under the user's
   per-conversation skill workspace. ``workspace_conv`` carries the
   conversation id so the file can be located inside the chat-scoped
   workspace tree (``users/<user>/conversations/<conv>/<skill>/<path>``).
   When ``workspace_conv`` is empty, the file lives in the legacy
   per-user shape (``<user>/<skill>/<path>``); the download handler
   falls back to that location for attachments persisted before
   conversation-scoped workspaces existed.

   ``data`` / ``text`` are empty; the frontend fetches the bytes on demand
   via the ``skills.workspace.download`` WebSocket RPC when the user clicks
   the download button. This is the mode tools use when they generate a
   file (PDF, image, spreadsheet, …) and want to make it downloadable from
   the assistant's reply without bloating the conversation row with
   base64 payloads that may run to megabytes.

The two modes are distinguished by whether ``workspace_path`` is set. Code
that needs to materialize the bytes should check ``workspace_path`` first
and fall back to ``data`` / ``text``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FileAttachment:
    """A file attached to a chat message (user-uploaded or tool-produced).

    ``kind`` is the discriminator — it controls how backends translate the
    attachment into a provider-specific content block:

    - ``"image"``: raw base64 image bytes in ``data``
      (``image/png`` / ``image/jpeg`` / ``image/gif`` / ``image/webp``),
      with ``media_type`` set. Anthropic emits an ``image`` block.
    - ``"document"``: raw base64 document bytes in ``data`` (typically
      ``application/pdf``), with ``media_type`` set. Anthropic emits a
      ``document`` block.
    - ``"text"``: decoded UTF-8 content in ``text`` (no base64).
      ``media_type`` is a hint like ``text/markdown``. Text attachments
      are inlined into the prompt as ``## <name>\\n\\n<body>`` so the
      model can reference them by filename.
    - ``"file"``: raw base64 bytes of an arbitrary file in ``data``.
      Used for anything the AI can't read natively — .xlsx, .docx,
      .zip, .mp4, binaries, whatever — so the user can still upload
      it, see it as a download chip on their own message, and the
      model sees a text stub announcing the filename + size + mime
      type without trying to parse the contents. Think of it as
      "attached but opaque to the model." Falls back to
      ``application/octet-stream`` when the browser can't identify
      the type. Anthropic emits a plain text block describing the
      attachment, not a content block.

    For workspace-reference attachments (``workspace_path`` set), the
    backend-side rendering logic should treat them the same as inline
    attachments of the same ``kind`` — at send time, code that forwards an
    attachment to a provider (image/document block) is expected to
    materialize the bytes from disk if ``workspace_path`` is set, then use
    them as if they had arrived inline. Assistant-produced attachments are
    not currently sent back through the AI (there's no "assistant
    attachment" provider block that makes sense) so this matters only for
    user-origin attachments that happen to be reference-style, which today
    is not a thing — user uploads are always inline.

    ``name`` is the user-visible filename, always set for documents and
    text kinds; for images it's optional (historical images have none).
    """

    kind: str
    name: str = ""
    media_type: str = ""
    data: str = ""
    text: str = ""
    # Reference-mode fields: when set, the actual bytes live on disk in
    # the named skill workspace and the frontend fetches them via
    # ``skills.workspace.download`` (small) or ``GET /api/chat/download``
    # (large). Leave empty for inline attachments.
    workspace_skill: str = ""
    workspace_path: str = ""
    # Conversation id this file was generated for, or empty for legacy
    # attachments persisted before per-conversation workspaces. Used by
    # the download handler to pick the right workspace root.
    workspace_conv: str = ""
    # Decoded byte size of the file. Filled in at upload time for
    # reference-mode attachments (where we can't read ``data`` to find
    # out) so the UI can show a size label and the AI stub can quote
    # "1.2 GB" without having to stat the disk. Zero for inline
    # attachments that haven't had it set — callers can fall back to
    # ``len(base64.b64decode(data))`` in that case.
    size: int = 0
    # Entity ID in the workspace_files collection. When set, the
    # download handler can resolve the file via the registry instead
    # of reconstructing the path from workspace_skill/path/conv.
    # Empty for legacy attachments persisted before the file registry.
    workspace_file_id: str = ""

    @property
    def is_reference(self) -> bool:
        """True when this attachment points at a workspace file."""
        return bool(self.workspace_path)
