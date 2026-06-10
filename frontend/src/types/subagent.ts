/** DTO for a subagent type as managed by admins and returned by the WS RPC. */
export interface SubagentTypeDTO {
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  backend: string;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  /** "all" | "include" | "exclude" */
  tool_mode: string;
  tools: string[];
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
