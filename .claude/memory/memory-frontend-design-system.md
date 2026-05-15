# Frontend Design System — Technical Broadsheet

## Summary
Gilbert's SPA runs on a deliberate, distinctive design system codenamed **Technical Broadsheet** — refined dark admin aesthetic with editorial typography hierarchy. The full visual spec is at `frontend/DESIGN.md`; this memory captures the load-bearing rules and the implementation conventions so future sessions don't drift back into generic shadcn defaults.

## Details

### The three rules (do not break)

1. **Mono carries meaning.** Monospace signals "this is data, not prose" — IDs, paths, durations, version numbers, status pills, keyboard shortcuts. `<code>` for inline technical content. Reach for `font-mono` *only* when the content is technical; using it decoratively breaks the signal.
2. **Hairlines over fills.** Surface separation comes from 1px borders (`border-border`), not from background-color shifts. Cards are mostly transparent. No drop-shadow elevation. The only shadow allowed is on floating surfaces (Select / DropdownMenu / Dialog / Sheet popovers) and even there it's tight — no glassmorphism, no 24px blurs.
3. **One accent doing real work.** A single warm-amber signal color (`--signal`, `oklch(0.78 0.16 75)` in dark) carries active state, primary action, focus ring, current-route indicator. Used **sparingly** — one accent visible per screen is the target. Status colors (`--success` / `--warning` / `--destructive` / `--info`) are functional only, never decorative.

### Tokens (in `frontend/src/index.css`)

- Type scale is tighter than shadcn defaults — `text-sm` is 13px, `text-base` 14px. Body defaults to `text-sm`. Tabular numerics on by default at `<body>`.
- Fonts: **Geist Variable** (sans, body), **JetBrains Mono Variable** (mono, technical content). Both `@fontsource-variable/*`, self-hosted.
- Radius is capped at 8px — `--radius-xl`/`--radius-2xl`/`--radius-3xl`/`--radius-4xl` all alias to 8px so consumer-grade `rounded-2xl` tokens silently flatten. Admin tools should feel precise.
- Motion: 120ms (`--duration-fast`) hover/focus, 180ms (`--duration-base`) expand/collapse. `--ease-out` only. No springs, no bounce, no scroll-jacking.
- Utility classes: `.eyebrow` (uppercase mono section label), `.indeterminate-bar` (2px hairline loading bar, the system's "loading" pattern — used in place of centered spinners except at full-page boot).

### Primitives (in `frontend/src/components/ui/`)

Revised to the new vocabulary:
- **Button** — six semantic variants (default / outline / secondary / ghost / destructive / link). Default is still solid filled because **161 existing call sites** rely on it as the implicit primary action; new code should prefer `outline` as the workhorse (the doc comment in `button.tsx` says so). Sizes are tight (`h-7` default, `h-6` sm, `h-5` xs).
- **Card** — hairline border, 6px corners, no shadow. New `<CardEyebrow>` slot for uppercase-mono category labels above the title. `<CardFooter>` is hairline-divided and pulls to card edges — it's the in-card action-bar terminus.
- **Badge** — reoriented as a real status pill. Mono `text-[11px]`, **case is the caller's choice** (write `RUNNING` for shouty state, `3 messages` for quiet count — don't expect the badge to uppercase for you). State variants: `active` / `pending` / `success` / `warning` / `error` / `off`. Meta variants: `neutral` / `outline`. Optional `dot` prop adds a 6px semantic dot prefix. Legacy variants (`default` / `secondary` / `destructive` / `ghost` / `link`) preserved as aliases.
- **Input** — hairline-only, `h-7`, signal-color focus outline. New `mono` prop for technical-content fields (IDs, paths, secrets — pair with reveal toggle outside the primitive).
- **Select** + **DropdownMenu** — same hairline/popover vocabulary, eyebrow-styled group labels, signal-tinted item selection.
- **Tooltip** — drops the inverted-foreground speech-bubble look. Hairline popover + dense mono caption. Arrow removed.
- **Tabs** — default variant is now `line` (underline indicator + signal-color bar). The old chip-style stays as `pill` for view-mode toggles.
- **Dialog** + **Sheet** — hairline border, sharp 8px corners, darker backdrop. `<DialogFooter>` pulls to edges as a resolution band.
- **Switch** — new primitive. Hairline-bordered track when off, signal-filled when on, 16px high to fit admin density. Used by `<ConfigField>` and `ServiceToggles`.

### Layout components (in `frontend/src/components/layout/`)

- **`<PageHeader>`** — the canonical top-of-page block. Eyebrow + title + description + actions + hairline. Use on every route. Title supports JSX (e.g. inline avatar, inline code, breadcrumb-style eyebrow). Sub-component `<PageHeaderEyebrow>` exported for reuse outside the header.
- **`<StatusBar>`** — sticky "n unsaved changes / Save / Cancel" pattern. Three positions (`top` / `bottom` / `static`), two tones (`default` / `dirty` — dirty adds a 2px signal-color rail on the left edge).
- **`<SideNav>` + `<TopBar>`** — already documented in [Dashboard & Nav Structure](memory-dashboard-nav.md). SideNav is contextual (page override → group children → hidden); TopBar is primary nav.
- **`<PageSidebar>` / `usePageSidebar`** — primitive that lets any page take over the global `SideNav` body. Two-context split (state + api) so consumers of the setter don't re-render on content updates. **This is load-bearing** — a single combined context causes an infinite render loop that visibly freezes navigation. See [Dashboard & Nav Structure](memory-dashboard-nav.md) for the gory detail.

### Patterns codified in the spec but not (yet) primitives

Page header eyebrow, section header eyebrow (uppercase mono label + hairline divider), list row (32-40px tall, leading 16px icon, mono trailing meta), form field (label above, hint below, error inline replacing hint), empty state (mono uppercase label + sentence + optional secondary action), and the "color-rail on active row" identity indicator. All described in `frontend/DESIGN.md`.

### Where the system is applied

Every top-level page is on the system: Dashboard, Settings (full pilot — see [Settings UI](memory-settings-ui.md)), Inbox, MCP (3 pages), Security (8 sub-routes), Notifications, Plugins, Documents, Account, Scheduler, Entities (3), System, Usage, Proposals, Agents (list + detail + edit), Goals (list + war room), Chat (see [Chat Transcript](memory-chat-transcript.md)). `ScreensPage` is the only intentional skip — it's a full-bleed kiosk display.

### Plugin authoring contract

Plugin TS imports from `@/components/ui/*` like any core code. Don't introduce a new font, color, or radius token. Don't define a new `Button` / `Card` / `Badge` — if you need a variant the system doesn't provide, propose adding it to the primitive. The system's vocabulary is the contract.

## Related
- `frontend/DESIGN.md` — the canonical visual spec (typography scale, spacing, color rules, motion, recurring patterns)
- [Dashboard & Nav Structure](memory-dashboard-nav.md) — TopBar / SideNav / PageSidebar primitive
- [Settings UI](memory-settings-ui.md) — pilot architecture
- [Chat Transcript](memory-chat-transcript.md) — chat redesign
- [Plugin UI Extensions](memory-plugin-ui-extensions.md) — slots and registry plugins use
