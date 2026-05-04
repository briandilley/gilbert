/** Configuration system types for the Settings UI. */

/** A single dropdown option — either a plain string (label = value) or an
 * object with distinct ``value`` (stored) and ``label`` (displayed). The
 * labeled form is used by dynamic ``choices_from`` sources that have a
 * friendly display name (e.g. mailbox name vs. mailbox id). */
export type ConfigChoice = string | { value: string; label: string };

export interface ConfigParamMeta {
  key: string;
  type: "string" | "integer" | "number" | "boolean" | "array" | "object";
  description: string;
  default: unknown;
  restart_required: boolean;
  sensitive: boolean;
  choices: ConfigChoice[] | null;
  multiline: boolean;
  backend_param: boolean;
  /** True when the field stores an AI prompt — the Settings UI renders an
   *  "Author with AI" button next to the textarea. */
  ai_prompt: boolean;
  /** Non-empty when this prompt is extensible by other plugins. The
   *  Settings UI shows a contributors list below the textarea, sourced
   *  from ``prompts.contributions.list`` for that target. Conventional
   *  value: ``"agent.system_prompt"``. */
  extensible_target?: string;
}

/** Normalize a ConfigChoice to its {value, label} form. */
export function normalizeChoice(c: ConfigChoice): { value: string; label: string } {
  return typeof c === "string" ? { value: c, label: c } : c;
}

export interface ConfigActionMeta {
  key: string;
  label: string;
  description: string;
  backend_action: boolean;
  /** Name of the backend this action belongs to; empty means service-level. */
  backend: string;
  confirm: string;
  required_role: string;
  hidden: boolean;
}

export interface ConfigActionResult {
  status: "ok" | "error" | "pending";
  message: string;
  open_url: string;
  followup_action: string;
  data: Record<string, unknown>;
}

export interface ConfigActionInvokeResponse {
  namespace: string;
  key: string;
  result: ConfigActionResult;
}

export interface ConfigSection {
  namespace: string;
  service_name: string;
  enabled: boolean;
  started: boolean;
  failed: boolean;
  params: ConfigParamMeta[];
  values: Record<string, unknown>;
  actions?: ConfigActionMeta[];
}

export interface ConfigCategory {
  name: string;
  sections: ConfigSection[];
}

export interface ConfigDescribeResponse {
  categories: ConfigCategory[];
}

export interface ConfigSectionResponse {
  namespace: string;
  params: ConfigParamMeta[];
  values: Record<string, unknown>;
}

export interface ConfigSetResult {
  namespace: string;
  results: Record<string, { status: string; message?: string; path?: string }>;
}
