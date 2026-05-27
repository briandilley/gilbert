import { apiFetch } from "./client";

export interface ScreensInfo {
  enabled: boolean;
  allow_guest_screens: boolean;
}

/** Public probe — never 401s — used to gate the screens + login page UI. */
export async function fetchScreensInfo(): Promise<ScreensInfo> {
  return apiFetch<ScreensInfo>("/screens/info");
}
