/**
 * Shared ``visible_when`` predicate for ConfigParam render loops.
 *
 * Multiple SPA surfaces iterate ``ConfigParam`` lists for the same
 * backend types — the Settings page (``ConfigSection``), the mailbox /
 * calendar / tasks editors, the system service card. They all need
 * the same filter so a backend that declares ``visible_when_field``
 * on a param hides it consistently everywhere.
 *
 * Sibling resolution: for a param keyed ``backends.gmail.X`` with
 * ``visible_when_field="credential_mode"`` we look up
 * ``backends.gmail.credential_mode`` (swap the trailing path segment).
 * Bare-key configs (``credential_mode`` directly at the top of
 * ``backendConfig``) work without modification because the swap
 * leaves single-segment keys alone.
 *
 * Stringified comparison so boolean / integer sibling fields work
 * uniformly — backends pass ``("true",)`` / ``("42",)`` as
 * appropriate.
 */

import type { ConfigParamMeta } from "@/types/config";

export function isParamVisible(
  param: ConfigParamMeta,
  values: Record<string, unknown>,
): boolean {
  const field = param.visible_when_field;
  if (!field) return true;
  const allowed = param.visible_when_values ?? [];
  if (allowed.length === 0) return false;
  const siblingKey = param.key.includes(".")
    ? param.key.replace(/[^.]+$/, field)
    : field;
  const currentRaw = values[siblingKey];
  const current = currentRaw == null ? "" : String(currentRaw);
  return allowed.some((v) => String(v) === current);
}
