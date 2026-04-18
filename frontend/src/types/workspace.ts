export interface WorkspaceFile {
  _id: string;
  conversation_id: string;
  category: "upload" | "output" | "scratch";
  filename: string;
  original_name: string;
  rel_path: string;
  media_type: string;
  size: number;
  skill_name?: string;
  created_at: string;
  created_by: "user" | "ai";
  description?: string;
  pinned: boolean;
  derived_from?: string;
  derivation_method?: string;
  derivation_script?: string;
  derivation_notes?: string;
  reusable?: boolean;
  metadata?: Record<string, unknown>;
}
