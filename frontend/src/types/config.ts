/** Configuration system types for the Settings UI. */

export interface ConfigParamMeta {
  key: string;
  type: "string" | "integer" | "number" | "boolean" | "array" | "object";
  description: string;
  default: unknown;
  restart_required: boolean;
  sensitive: boolean;
  choices: string[] | null;
  multiline: boolean;
  backend_param: boolean;
}

export interface ConfigSection {
  namespace: string;
  service_name: string;
  enabled: boolean;
  started: boolean;
  failed: boolean;
  params: ConfigParamMeta[];
  values: Record<string, unknown>;
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
