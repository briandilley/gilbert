import { apiFetch } from "./client";
import type {
  InboxStats,
  InboxMessage,
  MessageDetail,
  PendingReply,
} from "@/types/inbox";

export async function fetchInboxStats(): Promise<InboxStats> {
  return apiFetch("/inbox/api/stats");
}

export async function fetchMessages(params?: {
  sender?: string;
  subject?: string;
}): Promise<InboxMessage[]> {
  const qs = new URLSearchParams();
  if (params?.sender) qs.set("sender", params.sender);
  if (params?.subject) qs.set("subject", params.subject);
  const q = qs.toString();
  const resp = await apiFetch<{ messages: InboxMessage[]; total: number }>(
    `/inbox/api/messages${q ? `?${q}` : ""}`,
  );
  return resp.messages;
}

export async function fetchMessageDetail(id: string): Promise<MessageDetail> {
  return apiFetch(`/inbox/api/messages/${id}`);
}

export async function fetchThread(threadId: string): Promise<InboxMessage[]> {
  return apiFetch(`/inbox/api/threads/${threadId}`);
}

export async function fetchPending(): Promise<PendingReply[]> {
  const resp = await apiFetch<{ pending: PendingReply[] }>("/inbox/api/pending");
  return resp.pending;
}

export async function cancelPending(replyId: string): Promise<void> {
  await apiFetch(`/inbox/api/pending/${replyId}/cancel`, { method: "POST" });
}
