/**
 * Agent-action handler registry.
 *
 * The agent's ``request_user_input`` tool can attach an ``actions`` list
 * to the question — each action carries a ``kind`` string (e.g.
 * ``"open-url"`` or ``"browser.vnc"``) and a JSON ``payload``. The
 * agent chat UI renders one button per action and looks up the
 * matching handler here on click.
 *
 * - **Built-in handlers** are pre-registered below (``open-url``).
 * - **Plugin handlers** register via ``<plugin>/frontend/panels.ts``
 *   side-effect imports — same pattern as registerPanel for UI panels.
 *
 * Buttons whose ``kind`` has no registered handler render disabled
 * with a tooltip — better UX than silently dropping them.
 */

export type AgentActionPayload = Record<string, unknown>;

export type AgentActionHandler = (payload: AgentActionPayload) => void;

const _handlers = new Map<string, AgentActionHandler>();

export function registerAgentActionHandler(
  kind: string,
  handler: AgentActionHandler,
): void {
  if (_handlers.has(kind)) {
    console.warn(
      `Agent action handler "${kind}" registered twice — keeping the latest.`,
    );
  }
  _handlers.set(kind, handler);
}

export function getAgentActionHandler(
  kind: string,
): AgentActionHandler | null {
  return _handlers.get(kind) ?? null;
}

// ── Built-in handlers ──────────────────────────────────────────────

/** ``open-url`` — payload.url opens in a new tab. */
registerAgentActionHandler("open-url", (payload) => {
  const url = String(payload?.url || "").trim();
  if (!url) return;
  window.open(url, "_blank", "noopener,noreferrer");
});
