/**
 * Shapes returned by the ``usage.*`` WS RPCs. Mirrors
 * ``src/gilbert/interfaces/usage.py`` on the server.
 */

/** One row in a ``usage.query`` response — either a raw round (when
 *  no ``group_by`` was specified) or the aggregate for a group. */
export interface UsageAggregate {
  dimensions: Record<string, string>;
  rounds: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
}

/** Filter + aggregation spec sent to ``usage.query``. */
export interface UsageQueryPayload {
  /** ISO8601 start timestamp, inclusive. */
  start?: string;
  /** ISO8601 end timestamp, exclusive. */
  end?: string;
  user_id?: string;
  conversation_id?: string;
  backend?: string;
  model?: string;
  profile?: string;
  tool_name?: string;
  /** Any subset of ``user_id``, ``user_name``, ``backend``, ``model``,
   *  ``profile``, ``conversation_id``, ``tool_name``, ``date``,
   *  ``invocation_source``. Empty → no grouping, returns one row per
   *  matching AI round. */
  group_by?: string[];
}

export type UsageGroupBy =
  | "user_id"
  | "user_name"
  | "backend"
  | "model"
  | "profile"
  | "conversation_id"
  | "tool_name"
  | "date"
  | "invocation_source";

/** Dimensions catalog returned by ``usage.dimensions`` — populates the
 *  filter dropdowns from whatever has actually been seen in the
 *  collection, so there's never an empty dropdown and no dead entries
 *  for users/models that never used AI. */
export interface UsageDimensions {
  users: { user_id: string; user_name: string }[];
  backends: { backend: string }[];
  models: { backend: string; model: string }[];
  profiles: { profile: string }[];
  tools: { tool_name: string }[];
  invocation_sources: { source: string }[];
}
