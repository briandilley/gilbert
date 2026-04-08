import { apiFetch } from "./client";
import type {
  ConversationSummary,
  ConversationDetail,
  ChatResponse,
  ConversationMember,
} from "@/types/chat";

export async function fetchConversations(): Promise<ConversationSummary[]> {
  return apiFetch<ConversationSummary[]>("/chat/conversations");
}

export async function fetchConversation(
  id: string,
): Promise<ConversationDetail> {
  return apiFetch<ConversationDetail>(`/chat/conversations/${id}`);
}

export async function sendMessage(
  message: string,
  conversationId: string | null,
): Promise<ChatResponse> {
  return apiFetch<ChatResponse>("/chat/send", {
    method: "POST",
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
}

export async function renameConversation(
  id: string,
  title: string,
): Promise<void> {
  await apiFetch(`/chat/conversations/${id}/rename`, {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export async function deleteConversation(id: string): Promise<void> {
  await apiFetch(`/chat/conversations/${id}`, { method: "DELETE" });
}

export async function submitForm(
  conversationId: string,
  blockId: string,
  values: Record<string, unknown>,
): Promise<ChatResponse> {
  return apiFetch<ChatResponse>("/chat/form-submit", {
    method: "POST",
    body: JSON.stringify({
      conversation_id: conversationId,
      block_id: blockId,
      values,
    }),
  });
}

export async function createSharedRoom(
  title: string,
  visibility: "public" | "invite" = "public",
): Promise<{
  conversation_id: string;
  title: string;
  visibility: string;
  members: ConversationMember[];
}> {
  return apiFetch("/chat/shared", {
    method: "POST",
    body: JSON.stringify({ title, visibility }),
  });
}

export async function joinRoom(conversationId: string): Promise<void> {
  await apiFetch(`/chat/shared/${conversationId}/join`, { method: "POST" });
}

export async function leaveRoom(
  conversationId: string,
): Promise<{ status: string }> {
  return apiFetch(`/chat/shared/${conversationId}/leave`, { method: "POST" });
}

export async function kickMember(
  conversationId: string,
  userId: string,
): Promise<void> {
  await apiFetch(`/chat/shared/${conversationId}/kick`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}
