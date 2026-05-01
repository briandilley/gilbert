import { apiFetch } from "./client";

export interface UserMemory {
  memory_id: string;
  summary: string;
  content: string;
  source: "user" | "auto";
  created_at: string;
  updated_at: string;
  access_count: number;
}

export async function listMemories(): Promise<UserMemory[]> {
  return apiFetch<UserMemory[]>("/account/memories");
}

export async function deleteMemory(memoryId: string): Promise<void> {
  await apiFetch<void>(`/account/memories/${encodeURIComponent(memoryId)}`, {
    method: "DELETE",
  });
}

export async function clearMemories(): Promise<{ deleted: number }> {
  return apiFetch<{ deleted: number }>("/account/memories/clear", {
    method: "POST",
  });
}

export async function getMemoryOptOut(): Promise<{ opted_out: boolean }> {
  return apiFetch<{ opted_out: boolean }>("/account/memory-opt-out");
}

export async function setMemoryOptOut(optedOut: boolean): Promise<void> {
  await apiFetch<void>("/account/memory-opt-out", {
    method: "POST",
    body: JSON.stringify({ opted_out: optedOut }),
  });
}
