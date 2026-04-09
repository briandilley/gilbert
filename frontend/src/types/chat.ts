import type { UIBlock } from "./ui";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  author_id?: string;
  author_name?: string;
}

export interface ConversationSummary {
  conversation_id: string;
  title: string;
  preview: string;
  updated_at: string;
  message_count: number;
  shared: boolean;
  member_count?: number;
  members?: ConversationMember[];
  visibility?: "public" | "invite";
  is_member?: boolean;
  is_invited?: boolean;
}

export interface ConversationMember {
  user_id: string;
  display_name: string;
  role?: "owner" | "member";
}

export interface ConversationDetail {
  conversation_id: string;
  title: string;
  messages: ChatMessage[];
  ui_blocks: UIBlock[];
  updated_at: string;
  shared: boolean;
  members?: ConversationMember[];
  invites?: { user_id: string; display_name: string }[];
  owner_id?: string;
}

export interface ChatResponse {
  response: string;
  conversation_id: string;
  ui_blocks: UIBlock[];
}
