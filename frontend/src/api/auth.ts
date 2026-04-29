import { apiFetch } from "./client";
import type { User, LoginMethod } from "@/types/auth";

export async function fetchCurrentUser(): Promise<User> {
  return apiFetch<User>("/auth/me");
}

export async function fetchLoginMethods(): Promise<LoginMethod[]> {
  return apiFetch<LoginMethod[]>("/api/auth/methods");
}

export async function loginLocal(
  identifier: string,
  password: string,
): Promise<User> {
  return apiFetch<User>("/auth/login/local", {
    method: "POST",
    body: JSON.stringify({ identifier, password }),
  });
}

export async function logout(): Promise<void> {
  await fetch("/auth/logout", { method: "POST" });
}

export async function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<void> {
  await apiFetch<void>("/auth/password", {
    method: "POST",
    body: JSON.stringify({
      old_password: oldPassword,
      new_password: newPassword,
    }),
  });
}

export async function revokeAllSessions(): Promise<void> {
  await apiFetch<void>("/auth/sessions/revoke-all", { method: "POST" });
}
