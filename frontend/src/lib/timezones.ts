/**
 * Curated short-list of common IANA timezones for the dropdown. Keeps
 * the UI from becoming a 400-entry mega-menu while still covering the
 * vast majority of Gilbert installs. Users with a more exotic zone can
 * type it in the free-form input below the dropdown — anything the
 * server-side ``zoneinfo`` recognises is accepted.
 */
export const COMMON_TIMEZONES: string[] = [
  "UTC",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/New_York",
  "America/Phoenix",
  "America/Anchorage",
  "Pacific/Honolulu",
  "America/Sao_Paulo",
  "America/Mexico_City",
  "America/Toronto",
  "Europe/London",
  "Europe/Dublin",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Europe/Rome",
  "Europe/Amsterdam",
  "Europe/Stockholm",
  "Europe/Helsinki",
  "Europe/Warsaw",
  "Europe/Athens",
  "Europe/Istanbul",
  "Europe/Moscow",
  "Africa/Cairo",
  "Africa/Johannesburg",
  "Africa/Nairobi",
  "Asia/Dubai",
  "Asia/Karachi",
  "Asia/Kolkata",
  "Asia/Bangkok",
  "Asia/Shanghai",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Asia/Seoul",
  "Asia/Hong_Kong",
  "Australia/Perth",
  "Australia/Sydney",
  "Australia/Melbourne",
  "Pacific/Auckland",
];

/**
 * Every IANA zone the browser knows, for pickers that must offer any
 * zone. Falls back to the curated list on browsers without
 * ``Intl.supportedValuesOf``. ``current`` is always included so a saved
 * value the list lacks still shows up.
 */
export function allTimezones(current?: string): string[] {
  let zones: string[] = COMMON_TIMEZONES;
  try {
    const supported = (
      Intl as unknown as { supportedValuesOf?: (key: string) => string[] }
    ).supportedValuesOf?.("timeZone");
    if (supported && supported.length > 0) zones = ["UTC", ...supported];
  } catch {
    // keep the curated list
  }
  const set = new Set(zones);
  if (current) set.add(current);
  return [...set].sort((a, b) => (a === "UTC" ? -1 : b === "UTC" ? 1 : a.localeCompare(b)));
}
