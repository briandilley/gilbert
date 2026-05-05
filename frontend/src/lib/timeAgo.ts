/**
 * Format a UTC ISO timestamp as a short relative-time label, e.g.
 * "12s ago", "in 4h", "3d ago". Falls back to "never" for nullish input.
 */
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "—";
  const deltaSec = Math.round((Date.now() - ts) / 1000);
  const past = deltaSec >= 0;
  const abs = Math.abs(deltaSec);
  let body: string;
  if (abs < 60) body = `${abs}s`;
  else if (abs < 3600) body = `${Math.floor(abs / 60)}m`;
  else if (abs < 86400) body = `${Math.floor(abs / 3600)}h`;
  else body = `${Math.floor(abs / 86400)}d`;
  return past ? `${body} ago` : `in ${body}`;
}
