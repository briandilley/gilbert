# @-Mentions

## Summary

In shared chat rooms, members can `@`-mention each other and Gilbert. A mention is a structured markdown-link-shaped tag stored inline in message content; the SPA renders it as a styled chip, the backend records the resolved user-id list on the Message, fires a notification to each mentioned user, and the sidebar shows an unread-mentions badge per conversation. Browser-native (OS-level) notifications fire when the user is `@`-mentioned and isn't focused on that conversation.

Gilbert in rooms keeps the **legacy behavior** — bare-name "gilbert" detection still triggers the AI. The structured `@[Gilbert](gilbert)` mention is an additional, more deterministic way to address him.

## Mention syntax

Stored inline in message content as a markdown-link variant:

```
@[Display Name](user_id)
```

- **Why markdown-link-shaped**: round-trips through plaintext editing, escapable, recognizable to humans as "this is a structured reference," and easy to teach Marked.js to parse without breaking any of the existing markdown surface area.
- **Display name**: cosmetic only — rendered inside the chip. Renames don't strand historic mentions because the `user_id` is the stable anchor.
- **User id charset**: `[A-Za-z0-9._:-]+`. Tight enough that a malicious tag like `@[hi](rm -rf /)` doesn't slip through the parser; loose enough to accommodate UUIDs and namespaced ids.
- **Gilbert pseudo-id**: `gilbert` (constant `GILBERT_MENTION_USER_ID` in `core/chat.py`). Gilbert has no row in the user table — the SPA picker surfaces him alongside humans so the syntax is symmetric, and `mentions_gilbert()` matches both the legacy bare name and the structured tag.

## Backend

### Message field

`Message.mentioned_user_ids: list[str]` (in `interfaces/ai.py`). Resolved + validated list extracted at send time. `_serialize_message` writes it when non-empty; `_deserialize_message` reads it back. History replay reuses the persisted list — no per-load reparse.

### Send-path wiring

`AIService._ws_chat_send` (in `core/services/ai.py`) in the shared-room branch:

1. Builds the room's member id set.
2. Calls `filter_mentions_to_members(message, member_ids)` — returns `(valid_user_ids, mentions_gilbert_tag)`. Mentions pointing at non-members or self are silently dropped (a stale picker shouldn't fail the whole send).
3. **Non-addressed path** (no Gilbert mention, no slash command): builds the `Message` directly with `mentioned_user_ids=mentioned_ids`.
4. **Addressed path** (`mentions_gilbert()` or slash command): `chat()` builds and persists the `Message` internally without knowing about mentions; after it returns, `_stamp_mentions_on_last_user_message` retroactively writes the list onto the row. **Then** `_postprocess_assistant_mentions` rewrites the AI's reply: bare `@Name` tokens that match a room member (case-insensitive on display_name) become `@[Name](user_id)` with the member's canonical casing, the assistant message's content + `mentioned_user_ids` get updated, and humans Gilbert addressed are notified just like a human author had mentioned them. The rewritten reply is what comes back to the sender's WS response so the chip renders immediately (no plain-text-flash until next history reload).
5. Either way, `_notify_mentioned_users` fires a `notification.received` event per mentioned user (best-effort — exceptions logged at debug, not raised; the primary save already succeeded).

### AI-authored mentions

`resolve_bare_mentions_to_structured(content, members)` (in `core/chat.py`) is the post-processor for Gilbert's prose. Gilbert writes "@Root, what's up" — the picker that translates names into structured tags is on the SPA side and Gilbert doesn't use it. The rewrite is deterministic (regex + member lookup), idempotent on already-structured content, case-insensitive matching with canonical-casing output, and always accepts `@Gilbert` regardless of whether Gilbert is a member. Names that don't resolve to a known member pass through as plain text — we don't invent users from typos. In-word `@` (`alice@example.com`) is skipped via `(?<![\w@])` negative lookbehind.

### Notification payload

```json
{
  "source": "chat.mention",
  "message": "Alice mentioned you in Standup: Hey @Bob can you check this",
  "source_ref": {
    "conversation_id": "...",
    "author_id": "alice",
    "author_name": "Alice"
  }
}
```

The body text strips `@[Name](id)` syntax to bare `@Name` so OS-level toasts read naturally.

### Unread tracking

Index-based cursor on the member entry — `last_read_mention_index: int`. `conv_summary` (in `core/chat.py`) takes a `viewer_user_id` and counts messages newer than the cursor where the viewer's id appears in `mentioned_user_ids` and the message wasn't authored by them. `chat.conversation.mark_mentions_read` is a self-only WS RPC that advances the cursor to the latest message index; the SPA fires it whenever a shared conversation gains focus.

### Gilbert detection

`mentions_gilbert(message)` returns True if either:
- The legacy `\bgilbert\b` regex matches (case-insensitive), OR
- `extract_mentions(message)` includes the `gilbert` pseudo-id.

The legacy behavior is preserved on purpose: typing "gilbert, what's up" naturally still triggers the AI.

## Frontend

### Rendering

`MarkdownContent.tsx` adds a Marked.js inline extension that recognizes `@[Name](id)` and emits `<span class="mention" data-user-id="id">@Name</span>`. DOMPurify is configured with `ADD_ATTR: ["data-user-id"]` so the attribute survives sanitization.

CSS in `index.css` styles `.mention` as a neutral chip; a `useEffect` in MarkdownContent tags chips matching the viewer's user id with the `.mention-self` class — that's what the signal-amber accent rule selects on. The class-based approach is necessary because static CSS can't bake a per-user attribute-value selector.

### Picker

`components/chat/mentionPicker.ts` holds the pure detection logic:

- `detectMentionAtCursor(text, cursor)` — finds the in-progress `@query` immediately to the left of the caret, with escape conditions: in-word `@` (looks like an email), `@` inside a fenced code block, or a query that's accidentally captured whitespace.
- `filterMembers(members, query)` — substring match with prefix-first ranking.
- `renderMentionMarkup(member)` — produces the `@[Name](id) ` insertion (note the trailing space).

`ChatInput.tsx` wires it in alongside the existing slash-command picker:

- New `mentionableMembers?` prop — populated by ChatPage from the room's member list, plus the `Gilbert` pseudo-user. Personal chats pass `undefined` and the picker stays inert.
- Cursor position tracked via `onKeyUp`/`onClick`/`onSelect` so the detector sees fresh state without polling the DOM each render.
- Picker popover renders above the textarea (same shell as the slash popover), arrow keys / Enter / Tab navigate + insert, Escape inserts a space after the `@` to break the trigger.

### Sidebar badge

`ChatSidebar.tsx` reads `conv.unread_mentions_count` from each `ConversationSummary` and renders a signal-amber `@N` Badge before the member-count chip. Title goes semibold while the count is non-zero. The badge disappears when `mark_mentions_read` fires (on conversation focus).

### Browser notifications

`hooks/useBrowserNotifications.tsx`:

- Subscribes to `notification.received` events.
- Filters to `source === "chat.mention"` and matches the viewer's user id.
- Suppresses notifications for the conversation the user is currently viewing (visible tab + matching `active_conversation_id`).
- **Lazy permission**: first mention triggers `Notification.requestPermission()`. Up-front prompts are a known UX anti-pattern; the SPA bell remains as a fallback for users who deny permission.
- `tag` groups successive mentions from the same room so a busy chat doesn't pile up OS toasts.
- Click handler focuses the tab and deep-links to `/c/<conversation_id>` via the hash router.

Mounted once near the ChatPage root.

## Edge cases handled

- **In-word `@`** (email-like `foo@bar`) — picker doesn't trigger.
- **Code blocks** — `insideFencedCodeBlock` count of ``` openers in the prefix; odd = inside, skip.
- **Self-mention** — filtered out of both the picker pool and the post-validation list (won't notify self, won't badge self).
- **Stale mentions** — picker may suggest a user who left the room between page-load and send. The backend's `filter_mentions_to_members` silently drops them at send time.
- **Cross-room leakage** — mentions pointing at users outside the current room are dropped on the backend; the picker only surfaces members of the current room (plus Gilbert).

## Scope cuts (deferred)

- `@everyone` / `@room` — semantics + permission gating need more thought.
- Mention search / "mentions of me" inbox — `notification.received` events already include `source_ref.conversation_id` so this is mostly a UI surface waiting to be built.
- Per-chat granularity for browser notification preference. Single global "on if permission granted" flag for now.
- Editing mentions in-place after send. Edit/delete on messages isn't a feature yet anywhere in the chat product.

## Related

- `core/chat.py` — parser, validator, Gilbert detector, `conv_summary` unread tracking.
- `core/services/ai.py` — send-path wiring + `mark_mentions_read` RPC.
- `core/services/notifications.py` — `notify_user` API used by the dispatch.
- `interfaces/ai.py` — `Message.mentioned_user_ids` field.
- `frontend/src/components/chat/mentionPicker.ts` — pure picker detection logic.
- `frontend/src/components/ui/MarkdownContent.tsx` — Marked extension + DOMPurify allowlist + own-mention class tagging.
- `frontend/src/hooks/useBrowserNotifications.tsx` — OS notification dispatch + permission flow.
- `tests/unit/test_chat_mentions.py` — parser + filter + Gilbert detector + unread count tests.
