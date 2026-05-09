export interface User {
  user_id: string;
  email: string;
  display_name: string;
  roles: string[];
  provider: string;
  has_password: boolean;
  /**
   * IANA timezone the user picked on the Account page (e.g.
   * ``"America/Los_Angeles"``). ``null`` means "not configured" — the
   * server falls back to its own zone for time-of-day features
   * (push-notification quiet hours, daily summaries, …) and warns once
   * per user.
   */
  tz: string | null;
}

export interface LoginMethod {
  provider_type: string;
  display_name: string;
  method: "form" | "redirect";
  redirect_url: string;
  form_action: string;
}

export function isAuthenticated(user: User | null): boolean {
  return user !== null && user.user_id !== "system" && user.user_id !== "guest";
}

export function hasRole(user: User | null, role: string): boolean {
  if (!user) return false;
  if (user.roles.includes("admin")) return true;
  return user.roles.includes(role);
}
