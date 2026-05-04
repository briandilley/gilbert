# Browser Plugin Verification Findings

## 0.1 Workspace attachment lifecycle for tool-produced files

**No public "bytes-in, FileAttachment-out" helper exists.** The `_tool_attach_workspace_file` method (`src/gilbert/core/services/workspace.py:1878-2006`) is the canonical recipe, but it is a private tool executor that takes a `path` already on disk — it does not accept raw bytes. A plugin therefore composes the lifecycle out of the public `WorkspaceProvider` primitives.

**Capability protocol exists.** `WorkspaceProvider` lives at `src/gilbert/interfaces/workspace.py:10` and is `@runtime_checkable`. Std-plugins must use it via:

```python
from gilbert.interfaces.workspace import WorkspaceProvider
ws = resolver.get_capability("workspace")
if not isinstance(ws, WorkspaceProvider):
    return ToolResult(..., is_error=True)
```

(see `src/gilbert/core/services/ai.py:2966-2969` and `src/gilbert/web/routes/chat_uploads.py:104-119` for the canonical pattern). The protocol exposes `get_output_dir(user_id, conv_id) -> Path` (line 36) and `register_file(...) -> dict` (line 50) — exactly the two pieces a screenshot tool needs.

**Recipe for a PNG screenshot tool** (mirroring `_tool_attach_workspace_file` lines 1948-1996):

```python
user_id = arguments["_user_id"]                          # injected by AIService
conv_id = arguments["_conversation_id"]                  # ai.py:3328-3334, 3679-3689
out_dir = ws.get_output_dir(user_id, conv_id)            # creates outputs/
dest = out_dir / "screenshot.png"                        # de-dupe with -1, -2 if exists
dest.write_bytes(png_bytes)
entity = await ws.register_file(
    conversation_id=conv_id, user_id=user_id,
    category="output", filename=dest.name,
    rel_path=f"outputs/{dest.name}", media_type="image/png",
    size=len(png_bytes), created_by="ai",
)
attachment = FileAttachment(
    kind="image", name=dest.name, media_type="image/png",
    workspace_skill="workspace",                # literal string, not a skill name
    workspace_path=f"outputs/{dest.name}",      # POSIX, relative to workspace_root
    workspace_conv=conv_id,
    workspace_file_id=entity["_id"],
    size=len(png_bytes),
)
return ToolResult(tool_call_id="", content="Captured screenshot.",
                  attachments=(attachment,))
```

**Tool-args injection.** The AI service writes `_user_id` and `_conversation_id` into the arguments dict before dispatch (`src/gilbert/core/services/ai.py:3328-3334` and `:3679-3689`). The plugin's tool reads both directly; it does NOT declare them as `ToolParameter`s — they are stripped from the schema sent to the model and re-injected at execution time.

**FileAttachment shape.** `kind="image"` triggers Anthropic image-block rendering, but for reference-mode the bytes are loaded from disk at send time (`src/gilbert/interfaces/attachments.py:72-81`). `workspace_skill` is the literal `"workspace"` string (not the plugin name); `workspace_path` is POSIX and relative to `get_workspace_root()`; `workspace_conv` MUST be set so the download handler picks the conversation-scoped tree (`attachments.py:17-25`); `workspace_file_id` is the `_id` returned by `register_file()` and lets the download handler resolve via the registry instead of reconstructing the path.

## 0.2 Per-user UserContext propagation into ToolProvider
TBD

## 0.3 ConfigParam options for binary toggles + `restart_required` on Service plugins
TBD

## 0.4 Static-asset serving from a std-plugin directory
TBD

## 0.5 Encrypted-at-rest patterns already in core
TBD

## 0.6 Existing capability protocol candidates for the credential store
TBD

## 0.7 Playwright headless requirements (system packages, browser binary)
TBD
