# Source Inspector Service

## Summary
Read-only AI tools that let an AI session inspect Gilbert's own source tree. Built primarily so the proposals reflector can ground self-improvement suggestions in the actual current code, but exposed as a normal `ai_tools` provider so any AI profile can opt in.

## Details

### Core file
- `src/gilbert/core/services/source_inspector.py` — `SourceInspectorService`
- Registered in `core/app.py` *before* `ProposalsService` so the reflector can resolve it via `get_capability("source_inspector")`.

### Capability declarations
- `capabilities = {"source_inspector", "ai_tools"}`
- `optional = {"configuration"}`
- `toggleable = True`

### Tools (all admin-only, `parallel_safe=True`)
| Name | Purpose |
|---|---|
| `gilbert_list_files` | List entries inside a repo-relative path. Empty path lists the configured allowlist roots. |
| `gilbert_read_file` | Read a single file with a size cap (`max_file_bytes`, default 200 KB). Refuses binary extensions. |
| `gilbert_grep` | Regex search across the allowlist (or a sub-path). Caps match count and files scanned. |

All three return JSON strings (since `execute_tool` returns `str`). The repo root is auto-discovered by walking up from cwd looking for `src/gilbert/`; tests pass an explicit `repo_root` to sandbox.

### Path safety
`_check_path` resolves symlinks BEFORE checking the allowlist so `foo -> /etc/passwd` style escapes can't bypass it. The walker re-resolves each child during traversal so symlinked subtrees can't escape per-iteration. Cache/build dirs (`__pycache__`, `node_modules`, `.venv`, etc.) and binary file extensions are skipped entirely.

### Two tool-discovery paths
- `get_tools(user_ctx)` — the standard `ToolProvider` method. Returns `[]` when `enabled` is False so the inspector vanishes from any AI profile that's discovering tools.
- `get_tool_definitions()` — always returns the tool list regardless of `enabled`. Used by `ProposalsService._resolve_inspector_tools` to inject the inspector into the reflection AI call even when the user-facing toggle is off. The proposals service has already decided the call is appropriate; the user-facing toggle controls visibility to other AI profiles.

### Configuration (namespace `source_inspector`, category `Intelligence`)
- `enabled` (bool, default true) — controls visibility to ordinary AI profiles only; the proposals reflector still gets the tools either way.
- `allowed_paths` (array) — repo-relative directories or single files the AI may read. Defaults: `src`, `std-plugins`, `local-plugins`, `installed-plugins`, `frontend/src`, `tests`, `scripts`, `pyproject.toml`, `uv.lock`, `README.md`, `CLAUDE.md`, `gilbert.sh`, `.claude/memory`. `.gilbert/` (runtime data, possibly secrets) and `.git/` are deliberately omitted.
- `max_file_bytes` (int, default 200_000) — read truncation cap.
- `max_list_entries` (int, default 500)
- `max_grep_matches` (int, default 200)
- `max_grep_files` (int, default 2000)

### Why a service instead of bundling into ProposalsService
- Other AI profiles (e.g., the dev assistant profile) benefit from the same tools.
- Service boundary makes the path-allowlist a single source of truth and keeps proposals' tool-loop logic small.
- Keeps the layer rules clean: the inspector imports only from `interfaces/`, never from `core/services/`.

### Design notes
- Strictly read-only — there are no write/exec tools and the codebase rule is to keep it that way. Any "apply this change" capability belongs in a different, more carefully-gated service.
- The reflection AI runs on the most expensive profile, so output bounds are aggressive: a 10 MB file or a runaway grep would otherwise blow the context window and the bill.
- `execute_tool` returns `str` per the `ToolProvider` protocol — the dict result is JSON-serialized so the AI sees structured fields (path, content, truncated, etc.).

## Related
- `core/services/source_inspector.py`, `core/services/proposals.py` (consumer), `interfaces/tools.py`
- [Proposals Service](memory-proposals-service.md), [Capability Protocols](memory-capability-protocols.md), [Service System](memory-service-system.md)
