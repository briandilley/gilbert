export interface Role {
  name: string;
  level: number;
  description: string;
  builtin: boolean;
}

export interface ToolPermission {
  provider: string;
  tool_name: string;
  default_role: string;
  effective_role: string;
  has_override: boolean;
}

export interface AIProfile {
  name: string;
  description: string;
  tool_mode: "all" | "include" | "exclude";
  tools: string[];
  tool_roles: Record<string, string>;
  assigned_calls: string[];
  backend: string;
  model: string;
}

export interface UserRoleAssignment {
  user_id: string;
  username: string;
  email: string;
  display_name: string;
  roles: string[];
}

export interface CollectionACL {
  collection: string;
  read_role: string;
  write_role: string;
  has_custom: boolean;
}
