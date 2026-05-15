# Chat Transcript Design

## Summary
The chat UI (`/chat`) is intentionally **not** a peer-conversation product. It's a **work transcript** — left-aligned rail rows for both user and assistant, signal-amber identity bar for Gilbert, **mono-rail tool calls** that are visually distinct from the AI's prose. Bubbles, big avatars, and chatty warmth are out; skimmability, mono-as-data, and tool-call clarity are in. Gilbert spends most chat turns doing tool-heavy work, so the transcript optimizes for "easy to scan + intervene" over "feels personal."

## Details

### The design call (load-bearing)

Bubble-style chat (iMessage / Discord DMs) was the previous look. It was replaced because:
- Most "messages" are the AI doing work — tool calls, file outputs, structured responses.
- Bubbles zig-zag the eye (user right, AI left) which is fine for short back-and-forth but hostile to skimming a long AI turn with 10 tool calls.
- Avatars cost vertical real estate without earning it when the column is already labeled with the rail.

Don't reintroduce bubbles unless the chat product fundamentally shifts toward casual conversation.

### Turn structure (`TurnBubble.tsx`)

Each `ChatTurn` (user message + assistant response) renders as **two rail rows** stacked vertically:

```
▌ You · ...                    (user row — foreground/30 rail)
  attachment chips
  user message content

▌ Gilbert · ...                (assistant row — --signal rail)
  ThinkingCard (rounds + tools, collapsed by default)
  FinalAnswer (markdown, no bubble)
```

The `<TurnRail>` helper does both. Props: `toneClass` (rail bg color), `author` (display name), `authorClass` (text color for the name), optional `authorMeta` (JSX appended to the header — usage chip, interrupted indicator). The rail is a 2px vertical bar absolutely positioned on the left edge of a `pl-4`-indented body. Max-width 3xl (~760px) for prose readability.

User row tone: `bg-foreground/30` (neutral muted).
Assistant row tone: `bg-(--signal)` (signal amber, same accent the design system uses for "active / primary").
Name color: assistant's "Gilbert" label is `text-(--signal)`, mirroring the rail.

### Tool calls — the mono-rail pattern

The chat redesign's signature gesture. **Each `ToolEntry` is a mono-rail block**, not a bordered card. Anatomy:

```
│ ▸ ⚡ tool_name                    (clickable header, single line, mono name)
│     arguments (expandable)
│     result (expandable)
```

- 2px vertical bar on the left, status-colored:
  - `bg-(--signal)/70` while running
  - `bg-destructive/70` on error
  - `bg-foreground/20` when done (neutral — done is the boring default)
- Indented `pl-3` from the surrounding prose. Click expands the args + result as separate inner collapsible sections (`<CollapsibleSection>`).
- Mono `text-foreground` tool name + status icon (LoaderIcon spinning, XIcon for error, CheckIcon for done).
- `error` indicator on the right uses mono uppercase tracking + destructive color.

The status color rides on the rail so the content stays quiet at rest. A turn with 12 successful tool calls + 1 error reads as a column of neutral rails with one red one — the eye lands on the failure immediately.

### `ThinkingCard` wrapper

Wraps the entire rounds list in a single collapsed-by-default card. The header is a one-line live preview (most recent tool name + total counts + most recent reasoning snippet) that pulses while streaming. Expanded body shows `<RoundView>` per round — each renders the round number + token-usage chip + reasoning text + tool list.

The wrapper itself is a hairline-bordered `bg-card/40` rounded box (lost the previous `bg-muted/30` muted-fill look). When live-streaming, the border switches to dashed `border-(--signal)/40` and the header animate-pulses.

### `FinalAnswer`

Markdown rendered inline in the assistant rail column — **no bubble**. Plain `<MarkdownContent>` directly. Hover-reveal copy button (positioned absolutely in the top-right of the markdown block) for round-tripping the raw Markdown back out.

### Streaming caret

The `.animate-caret-blink` utility in `index.css` is the system's "Gilbert is typing right now" indicator. Appended to the end of the most-recent non-empty round's reasoning text. No separate "typing…" banner.

### `MessageList.tsx`

Scroll behavior is non-trivial; documented inline in the file. Anchored-to-bottom detection via `ANCHOR_THRESHOLD_PX` (80px); `useLayoutEffect` re-pins on turn/block changes if anchored; `ResizeObserver` watches for late layout shifts (async image loads, UI block expansions). Don't break this.

UI blocks (`<UIBlockRenderer>`) flow inline between turns at their `response_index` position. See [UI Blocks](memory-ui-blocks.md).

### `ChatPage.tsx` top bar

Stays compact (small h-auto `border-b px-3 py-2`). Deliberately NOT on `<PageHeader>` — the chat content needs the vertical space, and PageHeader's chunkier styling (text-xl title + description line + bigger padding) is the wrong proportion for an in-content title strip. This is the one place in the app where the system's `<PageHeader>` is the wrong call.

### `ChatInput.tsx` — slash-command picker

Slash autocomplete picker (`<button>` rows in an `absolute bottom-full max-h-72 overflow-y-auto` popover) uses a **callback ref on the active row** to call `scrollIntoView({ block: "nearest" })` when arrow keys move the selection past the visible window. Without this, arrowing through a long picker can hide the selection off-screen. Don't refactor away.

### Identity rail color contract

The `--signal` accent (warm amber) is the assistant's identity color in the chat surface and the "active / primary" accent everywhere else. They're the same color on purpose — Gilbert IS the primary actor on every screen. If shared rooms get multi-user identity bars in the future, those should pull from `GROUP_COLORS` / `GROUP_ACCENT_BG` in `nav-shared.ts` so each participant's rail color matches their nav-group hue.

## Related
- `frontend/src/components/chat/TurnBubble.tsx` — turn rendering, ThinkingCard, ToolEntry, FinalAnswer
- `frontend/src/components/chat/MessageList.tsx` — scroll anchoring
- `frontend/src/components/chat/ChatInput.tsx` — composer + slash picker
- `frontend/src/components/chat/ChatPage.tsx` — page shell, compact internal header
- [Frontend Design System](memory-frontend-design-system.md) — the vocabulary the chat is in
- [UI Blocks](memory-ui-blocks.md) — interactive forms that flow between turns
- [Slash Commands](memory-slash-commands.md) — the picker's data source
