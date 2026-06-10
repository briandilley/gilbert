/** DTO for a subagent type as managed by admins and returned by the WS RPC. */
export interface SubagentTypeDTO {
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  /** AI profile name for model-agnostic selection; "" = use raw backend/model. */
  ai_profile: string;
  backend: string;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  max_rounds: number;
  max_wall_clock_s: number | null;
  /** "sync" | "background" */
  execution_mode: string;
  /** "inline" | "report_file" */
  deliver_as: string;
  enabled: boolean;
  built_in: boolean;
  icon: string;
}
