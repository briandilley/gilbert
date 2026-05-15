# Settings UI Architecture

## Summary
`/settings` is the design-system pilot — the page where the Technical Broadsheet vocabulary survives contact with the densest admin form in the app (~30 services × per-service config, plus secrets, AI prompts, and per-backend overrides). Architecture worth knowing: state is shared in `SettingsContext` so a top-of-page StatusBar can aggregate "N unsaved changes across M services"; AI-prompt fields render as a compact preview + `PromptEditorDialog` instead of inline textareas; the category nav is a left rail with URL-synced search.

## Details

### Layout (`SettingsPage.tsx`)

- `PageHeader` (eyebrow="ADMIN") across the top.
- Sticky **`StatusBar`** below the header — only renders when there are dirty edits. Aggregates `aggregate.totalFields` and `aggregate.dirtyNamespaces.length` from `useSettingsAggregate()`. Carries **Save all** + **Discard** actions. `tone="dirty"` adds a 2px signal-color rail on its left edge to draw the eye.
- Below: two-column flex row.
  - **Left rail** (desktop, w-56): vertical list of category names with mono count badges. Active row gets `bg-foreground/8` + a 2px signal-color accent bar — same vocabulary as `SideNav`. URL-synced via `?category=`. Search input at the top of the rail filters categories+sections by namespace / param key / description; URL-synced via `?q=`.
  - **Mobile** (below `md:`): no rail — falls back to a `Select` dropdown for the category. Search input goes away on mobile (rare to need on a phone-sized screen).
- Right pane: scrollable content area with the active category's `ConfigSection` cards stacked. Plus a `<PluginPanelSlot slot="settings.<category>" />` at the bottom so plugins can contribute admin panels.

### `SettingsContext` — shared dirty state

Two contexts on purpose (state + api), same anti-pattern dance as `PageSidebar`. The setter context value is `useState`'s stable setter reference — pages that consume only the setter never re-render on edits. The state context is consumed only where it's *read*.

State shape:
```
{
  dirty: Record<namespace, Record<paramKey, value>>,
  saveStatus: Record<namespace, { message, ok, at } | null>,
}
```

API surface (from `useSettingsApi()`):
- `setField(namespace, key, value)` — write into the right bucket
- `setFields(namespace, values)` — bulk write (used by `persist`-bearing config actions)
- `discard(namespace)` / `discardAll()`
- `saveNamespace(namespace)` — calls `api.setConfigSection`, invalidates the `["config"]` query, records save status
- `saveAll()` — fires every dirty namespace in parallel via `Promise.allSettled`
- `resetToDefaults(namespace)` — calls `api.resetConfigSection`
- `setSaveStatus(namespace, message, ok)` — auto-clears after 3s

Convenience hooks:
- `useSettingsSection(namespace)` — bundles a per-namespace slice + the api into one read for `ConfigSection` consumers
- `useSettingsAggregate()` — returns `{ dirtyNamespaces, totalFields, isDirty }` for the global StatusBar

### `ConfigSection` — one card per service namespace

Clickable Card header (eyebrow with mono namespace + humanized title + status pill via `Badge` state variants — `active dot` running, `error dot` failed, `off dot` disabled). Click to expand/collapse.

Inside, the body splits into:
- `enabled` param (the gate) at the top
- Service-level params
- `backend` selector
- Backend-specific params (`backend_param: true`) grouped into their own **inset `<Card size="sm">`** so the "this only applies when this backend is selected" boundary is visually obvious
- Actions block (config actions — `test_connection`, `link_calendar`, etc.) with the same backend-filtering and `persist`-from-action flow as before
- `<CardFooter>` with dirty status (`N unsaved changes` in mono signal-amber) + Reset + Save

State management is all through `useSettingsSection`. Per-section Save and the global Save all both ultimately call `api.setConfigSection(namespace, values)` via the same shared mutation.

Auto-expand on search match: `ConfigSection` accepts a `searchQuery` prop. When non-empty AND the query matches the namespace / param key / description, the section force-expands so the matched field is visible without an extra click.

### `ServiceToggles` — special-cased "Services" category

The `_services` pseudo-namespace exposes one boolean param per toggleable service. Renders as a single Card with hairline-divided list rows + a `<Switch>` on the right of each. Uses the same `useSettingsSection` plumbing so its dirty edits aggregate into the StatusBar.

### `ConfigField` — the field-type dispatcher

Boolean → `<Switch>` (uniform across the app — same primitive `<ServiceToggles>` uses). String + sensitive → `<Input mono>` + ghost icon-xs reveal button. String + choices → `<Select>`. String + multiline + non-AI-prompt → `<Textarea>`. Number → `<Input type="number">`. Array → tag editor. Array + choices → checkbox multi-select. Object → key-value editor.

`restart_required` indicator uses `<Badge variant="warning">restart-required</Badge>` inline next to the label.

### AI-prompt fields → `PromptEditorDialog`

`param.ai_prompt && param.multiline` is the trigger. Instead of a giant inline textarea, the field renders a **compact `PromptFieldPreview`** — first non-empty line + mono `N lines · M chars` meta + pencil icon. Click opens **`PromptEditorDialog`**: full-width modal (`sm:max-w-3xl`), 55vh textarea, mono. Inside the dialog there's an "Author with AI" button that opens the existing `AuthorPromptDialog` (the AI-rewrite flow). `disablePointerDismissal` so a misclick doesn't lose work; Escape and Cancel/Apply still work.

Non-`ai_prompt` multiline fields (e.g. `exclude_dirs`, `request_headers`) keep their inline textarea — they're short and benefit from being visible.

### Why this matters

The settings page is by far the densest admin surface in Gilbert. If the design system can land here without sacrificing usability, it lands everywhere. The state lift + global save bar pattern is reusable for any future page where multiple sections share dirty edits.

## Related
- `frontend/src/components/settings/SettingsPage.tsx` — page shell + rail + StatusBar
- `frontend/src/components/settings/SettingsContext.tsx` — shared dirty state, two-context split
- `frontend/src/components/settings/ConfigSection.tsx` — per-service card
- `frontend/src/components/settings/ConfigField.tsx` — field-type dispatcher
- `frontend/src/components/settings/PromptEditorDialog.tsx` — AI-prompt editor
- [Frontend Design System](memory-frontend-design-system.md) — vocabulary the page is in
- [Configuration Service](memory-configuration-service.md) — backend the page reads from
- [Config Actions](memory-config-actions.md) — the action button flow
- [AI Prompts Are Always Configurable](memory-ai-prompts-configurable.md) — why `ai_prompt: true` exists
