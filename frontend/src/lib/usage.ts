/**
 * Token + USD formatting helpers shared by the chat UI and the usage
 * reporting page. Kept alongside other presentational utilities so the
 * rules live in one place if we ever revisit them.
 */

export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) {
    const k = n / 1000;
    return k >= 10 ? `${k.toFixed(0)}K` : `${k.toFixed(1)}K`;
  }
  const m = n / 1_000_000;
  return m >= 10 ? `${m.toFixed(0)}M` : `${m.toFixed(2)}M`;
}

export function formatCost(usd: number): string {
  if (!Number.isFinite(usd) || usd <= 0) return "$0";
  // Below a cent, show three significant decimals so $0.00042 is
  // distinguishable from $0.00003. Above a cent, two decimals keeps
  // it readable (and matches the way prices are quoted).
  if (usd < 0.01) return `$${usd.toFixed(5).replace(/0+$/, "").replace(/\.$/, "")}`;
  if (usd < 1) return `$${usd.toFixed(3)}`;
  if (usd < 100) return `$${usd.toFixed(2)}`;
  return `$${usd.toFixed(0)}`;
}

/**
 * Compact "14.2K in · 512 out · $0.041" label. ``null``/``undefined``
 * usage returns an empty string so the caller can just skip rendering.
 */
export function summarizeUsage(
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    cache_creation_tokens?: number;
    cache_read_tokens?: number;
    cost_usd?: number;
  } | null | undefined,
  opts: { includeCache?: boolean } = {},
): string {
  if (!usage) return "";
  const parts: string[] = [];
  parts.push(`${formatTokens(usage.input_tokens ?? 0)} in`);
  parts.push(`${formatTokens(usage.output_tokens ?? 0)} out`);
  if (opts.includeCache) {
    const cr = usage.cache_read_tokens ?? 0;
    if (cr > 0) parts.push(`${formatTokens(cr)} cached`);
  }
  if ((usage.cost_usd ?? 0) > 0) {
    parts.push(formatCost(usage.cost_usd ?? 0));
  }
  return parts.join(" · ");
}
