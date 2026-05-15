# Gilbert design system — Technical Broadsheet

A short, opinionated style spec. Three load-bearing rules drive every
other decision; the rest of this document is the working-out.

## The three rules

1. **Mono carries meaning.** The presence of monospace signals "this
   is data, not prose" — IDs, paths, durations, version numbers,
   status pills, keyboard shortcuts. Don't reach for `font-mono`
   decoratively. Wrapping technical content in `<code>` is the lazy
   path and it works.

2. **Hairlines over fills.** Surface separation comes from 1px
   borders, not background-color shifts. Cards are mostly transparent;
   borders define them. This is what gives the UI its schematic,
   engineering-document feel — and it survives high density without
   reading as cluttered.

3. **One accent doing real work.** A single signal color
   (`--signal`, warm amber) carries active state, primary action,
   focus ring, current-route indicator. Used **sparingly** — typically
   one accent visible per screen. Status colors (success / warning /
   error / info) are functional only, never decorative.

When the design pushes back against these rules, prefer the rule.

---

## Typography

| Token       | Px / Line  | Use                                  |
|-------------|------------|--------------------------------------|
| `text-2xs`  | 11 / 1.35  | Eyebrows, tiny mono labels, badges   |
| `text-xs`   | 12 / 1.45  | Meta, captions, hints, table density |
| `text-sm`   | 13 / 1.5   | Body default                         |
| `text-base` | 14 / 1.55  | Comfortable body (chat, docs)        |
| `text-lg`   | 16 / 1.4   | Section titles                       |
| `text-xl`   | 19 / 1.3   | Page titles                          |
| `text-2xl`  | 24 / 1.25  | Rare — landing pages only            |

- **Sans** (`--font-sans`): *Geist Variable*. Default for everything
  except monospace.
- **Mono** (`--font-mono`): *JetBrains Mono Variable*. For technical
  data — IDs, paths, durations, versions, status pills, keyboard
  shortcuts.
- Display sizes get tracking `-0.015em`; uppercase labels get
  tracking `+0.06em` (`tracking-[0.06em]`).
- `tabular-nums` is on by default on `<body>`. Don't disable unless
  you need proportional figures for prose.

## Spacing

4px base. Use 4 / 8 / 12 / 16 / 24 / 32 / 48. Avoid 20 and 28 — they
feel uncalibrated. Large gaps (24+) are reserved for major section
breaks, not for between rows.

## Density

- **Compact** (default). Buttons `h-7` (28px), inputs `h-7`, list
  rows 32px, dialog padding 16px.
- **Comfortable**. Reserved for chat messages, long-form content,
  and the dashboard. Buttons `h-8`, body `text-base`.

Default to compact. The system is admin tooling first.

## Color rules

- Neutrals do ~95% of the work. Six-step grayscale ramp (background
  → foreground); use it.
- `--signal` (amber, `oklch(0.78 0.16 75)` in dark) is the single
  accent. Active selection, primary action, focus ring, "live now"
  indicator. **One visible per screen** is the target.
- Status colors are functional only:
  - `--success` — operation resolved OK
  - `--warning` — needs attention but not broken
  - `--destructive` — error state, dangerous action
  - `--info` — passive informational state
- Never decorative. A green dot on a status pill is fine; a green
  card background is not.
- The categorical nav-group colors (chat=blue, security=violet, etc)
  are the only allowed "decorative" colors, and only because they're
  encoding categorical meaning. Don't extend the practice.

## Corners

- `rounded-md` (6px) — default
- `rounded-sm` (4px) — small things (badges, xs buttons)
- `rounded-lg` (8px) — large surfaces (dialogs)
- Never `rounded-full` except for avatars and status dots
- Never `rounded-2xl` and up. Those tokens are aliased to 8px so
  consumer-y radii silently flatten.

## Surface hierarchy

Four levels, ordered by elevation:

1. **Canvas** — `bg-background`. The page itself. No border.
2. **Panel** — `bg-background` + `border border-border`. Carved
   region inside the canvas.
3. **Card** — `bg-card` + `border border-border`. Subtly elevated.
   Use `<Card>` primitive.
4. **Inline** — same row, separated by hairline. Often a `<ul>` of
   list rows divided by `divide-y divide-border`.

Glassmorphism, drop-shadow elevation, and large background-color
shifts are all out. Hairlines do this work.

## Motion

- `--duration-fast` (120ms) for hover/focus/state pings.
- `--duration-base` (180ms) for expand/collapse, dialog enter/exit.
- `--ease-out` always. No springs, no bounces, no snap-back.
- One staggered fade-in on initial page mount (60ms stagger, max 4
  elements). Don't add micro-interactions to every element.
- Loading is `.indeterminate-bar` — a 2px hairline at the top of the
  affected region. Centered spinners only for full-page or
  app-boot loading.

## Recurring patterns

### Page header

```
┌─────────────────────────────────────────────────┐
│  EYEBROW (uppercase mono, 11px)                  │
│  Title (19px sans, weight 600)                   │
│  Optional description (13px muted)               │
│                                       ┌── actions │
│  ───────────────── hairline ─────────  └── here   │
```

24px top padding, 16px bottom (between hairline and content).
Actions cluster right-aligned. Description sits between title and
hairline, indented to the same column as the title.

### Section header (inside a panel)

```
ACTIONS                                 3
───────────────────────────────────────
```

- `<span className="eyebrow">` — uppercase mono label, `text-2xs`.
- Optional `<Badge variant="neutral">` count on the right.
- Hairline below; `mt-4 mb-2` separates from siblings.

### List row

- 32px tall (compact) or 40px tall (with description / two-line).
- Leading icon (16px, `text-muted-foreground`).
- Label (`text-sm`).
- Trailing meta in mono (`font-mono text-xs text-muted-foreground`).
- Hover: `bg-foreground/4`. Selected: `bg-foreground/8` + 2px
  signal-colored left bar.

### Form field

```
Label                                    optional metadata
[input                                                   ]
Hint / error (12px muted or destructive)
```

- Label `text-xs font-medium` above input.
- Inline metadata (uppercase mono `text-2xs`) right-aligned with
  the label baseline.
- Hint below in `text-xs text-muted-foreground`.
- Errors replace the hint in `text-xs text-destructive`.

### Action bar

Sticky bottom of a card or page-level region. Hairline-divided
above the content.

```
─────────────────────────────────────────
  3 unsaved changes (mono accent)         [Cancel] [Save]
```

- Status text on the left, mono, signal-colored if active.
- Action buttons on the right. Last one is `variant="default"` (loud).
- Use `<CardFooter>` for the in-card version.

### Status pill

`<Badge variant="active" dot>RUNNING</Badge>`

- 18px tall, mono 11px, tracking-tight.
- Optional 6px dot (semantic-colored) prefix via `dot` prop.
- Variants map to state: `active` / `pending` / `success` /
  `warning` / `error` / `off`. Plus `neutral` and `outline` for
  meta/count.
- **Case is up to the caller.** Write `RUNNING` for shouty state
  pills; `3 messages` for quiet counts. Don't expect the badge to
  uppercase for you.

### Empty state

```
NO DOCUMENTS
There's nothing in this source yet.
[Index now]   ← optional secondary action
```

- Mono uppercase label first (`eyebrow` class), one line.
- Single sentence in `text-sm text-muted-foreground`.
- Optional `<Button variant="outline" size="sm">` action.
- Center-aligned, generous vertical padding (48-80px).

### Loading

- Per-region: `<div className="indeterminate-bar" />` at the top.
- Full-page: a centered `<LoadingSpinner>` is fine, but rare.
- Skeletons: subtle one-shade-lighter blocks, **no shimmer**. The
  shimmer animation belongs in consumer apps.

## What plugins should import

Plugin TS uses the `@/` alias and imports from `@/components/ui/*`
just like core. The contract is:

- `Button`, `Input`, `Card` + family, `Badge`, `Separator`,
  `Tooltip`, `Dialog`, `DropdownMenu`, `Sheet`, `Select`, `Tabs`,
  `ScrollArea`, `Avatar`, `Textarea`, `Label`.
- Don't define your own button / card / badge. If you need a variant
  the system doesn't provide, propose adding it here.
- Don't introduce a new font, color, or radius token. The system
  already has the answer; if it doesn't, the answer is one of
  "neutral" or "signal."

## Phase-2 pilot — Settings

Once these primitives have landed and survived a beat, the settings
UI is the pilot for applying them at the page level. The shape we're
aiming at:

- Replace the dropdown nav with a left rail of categories + global
  search.
- One global "unsaved changes — Save all (n)" action bar at the top.
- Per-section header uses the eyebrow pattern; sections become
  `<Card>` instances with `CardHeader` / `CardContent` / `CardFooter`.
- Backend selector pulls its scoped params into a labeled inset
  `<Card size="sm">` so the boundary between "service-level" and
  "this backend only" pops.
- AI-prompt fields become a single-line "View prompt" trigger that
  opens a dedicated editor dialog — they don't belong inline in a
  dense form.
- Secrets get a reveal toggle, and the underlying `<Input>` already
  supports `mono` for them.

That work happens in phase 2; nothing on this page is required to
preview it.
