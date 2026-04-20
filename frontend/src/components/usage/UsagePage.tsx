import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  Cell,
} from "recharts";
import { useWsApi } from "@/hooks/useWsApi";
import type {
  UsageAggregate,
  UsageDimensions,
  UsageGroupBy,
  UsageQueryPayload,
} from "@/types/usage";
import { Card } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatCost, formatTokens } from "@/lib/usage";
import { cn } from "@/lib/utils";
import { RotateCcwIcon } from "lucide-react";

type Metric = "cost_usd" | "input_tokens" | "output_tokens" | "total_tokens";

interface FiltersState {
  user_id: string;
  backend: string;
  model: string;
  profile: string;
  tool_name: string;
  invocation_source: string;
  /** Absolute YYYY-MM-DD day (inclusive). Empty string = no bound. */
  start: string;
  /** Absolute YYYY-MM-DD day (inclusive). Empty string = no bound. */
  end: string;
}

const EMPTY_FILTERS: FiltersState = {
  user_id: "",
  backend: "",
  model: "",
  profile: "",
  tool_name: "",
  invocation_source: "",
  start: "",
  end: "",
};

const GROUP_BY_OPTIONS: { value: UsageGroupBy; label: string }[] = [
  { value: "date", label: "Date" },
  { value: "user_name", label: "User" },
  { value: "backend", label: "Backend" },
  { value: "model", label: "Model" },
  { value: "profile", label: "Profile" },
  { value: "tool_name", label: "Tool" },
  { value: "invocation_source", label: "Source" },
  { value: "conversation_id", label: "Conversation" },
];

const METRIC_OPTIONS: { value: Metric; label: string }[] = [
  { value: "cost_usd", label: "Cost ($)" },
  { value: "input_tokens", label: "Input tokens" },
  { value: "output_tokens", label: "Output tokens" },
  { value: "total_tokens", label: "Total tokens" },
];

// Distinct, colorblind-ish palette cycling through so up to ~8 series
// stay readable in a stacked bar without relying on recharts' defaults.
const SERIES_COLORS = [
  "#2563eb", // blue
  "#16a34a", // green
  "#ea580c", // orange
  "#9333ea", // purple
  "#dc2626", // red
  "#0891b2", // cyan
  "#ca8a04", // amber
  "#db2777", // pink
];

// ── The page ─────────────────────────────────────────────────────────

export function UsagePage() {
  const api = useWsApi();

  const [filters, setFilters] = useState<FiltersState>(EMPTY_FILTERS);
  const [groupBy, setGroupBy] = useState<UsageGroupBy>("date");
  const [metric, setMetric] = useState<Metric>("cost_usd");

  const dimensionsQuery = useQuery<UsageDimensions>({
    queryKey: ["usage", "dimensions"],
    queryFn: () => api.listUsageDimensions(),
    staleTime: 60_000,
  });

  const queryPayload = useMemo<UsageQueryPayload>(() => {
    const payload: UsageQueryPayload = { group_by: [groupBy] };
    if (filters.user_id) payload.user_id = filters.user_id;
    if (filters.backend) payload.backend = filters.backend;
    if (filters.model) payload.model = filters.model;
    if (filters.profile) payload.profile = filters.profile;
    if (filters.tool_name) payload.tool_name = filters.tool_name;
    // ``invocation_source`` filtering isn't in the server query yet, so
    // we apply it client-side after the fetch. Keeping it out of the
    // payload avoids accidentally hitting an unknown filter field.
    if (filters.start) payload.start = `${filters.start}T00:00:00+00:00`;
    if (filters.end) {
      // End is inclusive in the UI; translate to exclusive-midnight the
      // next day for the server's < comparison.
      const next = new Date(`${filters.end}T00:00:00Z`);
      next.setUTCDate(next.getUTCDate() + 1);
      payload.end = next.toISOString();
    }
    return payload;
  }, [filters, groupBy]);

  const queryQuery = useQuery<UsageAggregate[]>({
    queryKey: ["usage", "query", queryPayload],
    queryFn: () => api.queryUsage(queryPayload),
    staleTime: 5_000,
  });

  const filteredRows = useMemo(() => {
    const rows = queryQuery.data ?? [];
    // Apply the not-server-side filters client-side.
    return rows.filter((r) => {
      if (
        filters.invocation_source &&
        r.dimensions.invocation_source !== filters.invocation_source
      ) {
        return false;
      }
      return true;
    });
  }, [queryQuery.data, filters.invocation_source]);

  const totals = useMemo(() => aggregateTotals(filteredRows), [filteredRows]);

  const canReset =
    Object.values(filters).some((v) => v !== "") ||
    groupBy !== "date" ||
    metric !== "cost_usd";

  return (
    <div className="max-w-screen-xl mx-auto p-4 md:p-6 space-y-5">
      {/* Header */}
      <header className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold">Usage</h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            AI token consumption and cost reporting
          </p>
        </div>
        {canReset && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setFilters(EMPTY_FILTERS);
              setGroupBy("date");
              setMetric("cost_usd");
            }}
          >
            <RotateCcwIcon className="size-3.5 mr-1.5" />
            Reset
          </Button>
        )}
      </header>

      {/* KPI strip */}
      <KpiStrip totals={totals} />

      {/* Filters + pickers */}
      <Card className="p-4 space-y-4">
        <DateRangeBar filters={filters} setFilters={setFilters} />
        <FilterRow
          filters={filters}
          setFilters={setFilters}
          dims={dimensionsQuery.data}
        />
        <div className="flex flex-wrap gap-4 items-end">
          <PickerField
            label="Group by"
            value={groupBy}
            options={GROUP_BY_OPTIONS.map((o) => ({
              value: o.value,
              label: o.label,
            }))}
            onChange={(v) => setGroupBy(v as UsageGroupBy)}
          />
          <PickerField
            label="Metric"
            value={metric}
            options={METRIC_OPTIONS.map((o) => ({
              value: o.value,
              label: o.label,
            }))}
            onChange={(v) => setMetric(v as Metric)}
          />
        </div>
      </Card>

      {/* Chart + table */}
      <Card className="p-4 space-y-4">
        {queryQuery.isLoading ? (
          <div className="h-64 flex items-center justify-center">
            <LoadingSpinner />
          </div>
        ) : queryQuery.isError ? (
          <div className="p-6 text-sm text-destructive">
            {(queryQuery.error as Error).message}
          </div>
        ) : filteredRows.length === 0 ? (
          <div className="p-12 text-center text-sm text-muted-foreground">
            No usage matches the current filters.
          </div>
        ) : (
          <>
            <ChartView
              rows={filteredRows}
              groupBy={groupBy}
              metric={metric}
            />
            <ResultsTable rows={filteredRows} groupBy={groupBy} />
          </>
        )}
      </Card>
    </div>
  );
}

// ── KPI strip ────────────────────────────────────────────────────────

function KpiStrip({
  totals,
}: {
  totals: ReturnType<typeof aggregateTotals>;
}) {
  const avgCost = totals.rounds > 0 ? totals.cost_usd / totals.rounds : 0;
  const items: { label: string; value: string; sub?: string }[] = [
    {
      label: "Cost",
      value: formatCost(totals.cost_usd),
      sub: `${totals.rounds} round${totals.rounds === 1 ? "" : "s"}`,
    },
    {
      label: "Input tokens",
      value: formatTokens(totals.input_tokens),
      sub: totals.cache_read_tokens
        ? `+ ${formatTokens(totals.cache_read_tokens)} cached`
        : undefined,
    },
    {
      label: "Output tokens",
      value: formatTokens(totals.output_tokens),
    },
    {
      label: "Avg cost / round",
      value: formatCost(avgCost),
    },
  ];
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
      {items.map((item) => (
        <Card key={item.label} className="p-3 gap-0.5">
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
            {item.label}
          </div>
          <div className="text-xl font-semibold tabular-nums">{item.value}</div>
          {item.sub && (
            <div className="text-[11px] text-muted-foreground tabular-nums">
              {item.sub}
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}

// ── Date range bar ───────────────────────────────────────────────────

function DateRangeBar({
  filters,
  setFilters,
}: {
  filters: FiltersState;
  setFilters: (f: FiltersState) => void;
}) {
  function presetDays(days: number) {
    const end = today();
    const start = new Date();
    start.setUTCDate(start.getUTCDate() - (days - 1));
    setFilters({
      ...filters,
      start: isoDay(start),
      end,
    });
  }
  function presetAll() {
    setFilters({ ...filters, start: "", end: "" });
  }
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="flex gap-1">
        <RangeChip label="Today" onClick={() => presetDays(1)} />
        <RangeChip label="7d" onClick={() => presetDays(7)} />
        <RangeChip label="30d" onClick={() => presetDays(30)} />
        <RangeChip label="All" onClick={presetAll} />
      </div>
      <div className="flex items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label className="text-[11px] text-muted-foreground">From</Label>
          <Input
            type="date"
            value={filters.start}
            onChange={(e) => setFilters({ ...filters, start: e.target.value })}
            className="h-8 text-xs w-[140px]"
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label className="text-[11px] text-muted-foreground">To</Label>
          <Input
            type="date"
            value={filters.end}
            onChange={(e) => setFilters({ ...filters, end: e.target.value })}
            className="h-8 text-xs w-[140px]"
          />
        </div>
      </div>
    </div>
  );
}

function RangeChip({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <Button variant="outline" size="sm" className="h-8 text-xs" onClick={onClick}>
      {label}
    </Button>
  );
}

// ── Filter row ───────────────────────────────────────────────────────

function FilterRow({
  filters,
  setFilters,
  dims,
}: {
  filters: FiltersState;
  setFilters: (f: FiltersState) => void;
  dims: UsageDimensions | undefined;
}) {
  const users = dims?.users ?? [];
  const backends = dims?.backends ?? [];
  const models = dims?.models ?? [];
  const profiles = dims?.profiles ?? [];
  const tools = dims?.tools ?? [];
  const sources = dims?.invocation_sources ?? [];

  // Narrow the model dropdown to the selected backend's models when a
  // backend is chosen — the default list blends backends together.
  const visibleModels = filters.backend
    ? models.filter((m) => m.backend === filters.backend)
    : models;

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2">
      <FilterSelect
        label="User"
        value={filters.user_id}
        onChange={(v) => setFilters({ ...filters, user_id: v })}
        options={users.map((u) => ({ value: u.user_id, label: u.user_name }))}
      />
      <FilterSelect
        label="Backend"
        value={filters.backend}
        onChange={(v) =>
          setFilters({
            ...filters,
            backend: v,
            // clear model if it no longer matches
            model:
              v && filters.model
                ? models.some((m) => m.backend === v && m.model === filters.model)
                  ? filters.model
                  : ""
                : filters.model,
          })
        }
        options={backends.map((b) => ({ value: b.backend, label: b.backend }))}
      />
      <FilterSelect
        label="Model"
        value={filters.model}
        onChange={(v) => setFilters({ ...filters, model: v })}
        options={visibleModels.map((m) => ({
          value: m.model,
          label: m.model,
        }))}
      />
      <FilterSelect
        label="Profile"
        value={filters.profile}
        onChange={(v) => setFilters({ ...filters, profile: v })}
        options={profiles.map((p) => ({ value: p.profile, label: p.profile }))}
      />
      <FilterSelect
        label="Tool"
        value={filters.tool_name}
        onChange={(v) => setFilters({ ...filters, tool_name: v })}
        options={tools.map((t) => ({ value: t.tool_name, label: t.tool_name }))}
      />
      <FilterSelect
        label="Source"
        value={filters.invocation_source}
        onChange={(v) => setFilters({ ...filters, invocation_source: v })}
        options={sources.map((s) => ({ value: s.source, label: s.source }))}
      />
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  // ``__any__`` is a sentinel value — the Select primitive requires a
  // non-empty value to render, so "All" uses a distinct token that we
  // translate back to an empty string in the change handler.
  const displayValue = value || "__any__";
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <Label className="text-[11px] text-muted-foreground truncate">
        {label}
      </Label>
      <Select
        value={displayValue}
        onValueChange={(v) => onChange(v === "__any__" ? "" : v ?? "")}
      >
        <SelectTrigger className="h-8 text-xs">
          <SelectValue placeholder="All" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="__any__">All</SelectItem>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function PickerField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <Label className="text-[11px] text-muted-foreground">{label}</Label>
      <Select value={value} onValueChange={(v) => v && onChange(v)}>
        <SelectTrigger className="h-8 text-xs w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

// ── Chart view ───────────────────────────────────────────────────────

function ChartView({
  rows,
  groupBy,
  metric,
}: {
  rows: UsageAggregate[];
  groupBy: UsageGroupBy;
  metric: Metric;
}) {
  // "date" grouping → timeseries area chart, ascending by date.
  // Everything else → categorical bar chart, sorted by the metric desc.
  if (groupBy === "date") {
    return <DateAreaChart rows={rows} metric={metric} />;
  }
  return <CategoryBarChart rows={rows} groupBy={groupBy} metric={metric} />;
}

function DateAreaChart({
  rows,
  metric,
}: {
  rows: UsageAggregate[];
  metric: Metric;
}) {
  const data = useMemo(() => {
    return [...rows]
      .map((r) => ({
        date: r.dimensions.date,
        value: metricValue(r, metric),
      }))
      .sort((a, b) => (a.date < b.date ? -1 : 1));
  }, [rows, metric]);

  return (
    <div className="h-72">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 10, right: 16, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="usageArea" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={SERIES_COLORS[0]} stopOpacity={0.45} />
              <stop offset="100%" stopColor={SERIES_COLORS[0]} stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis
            dataKey="date"
            stroke="var(--muted-foreground)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            stroke="var(--muted-foreground)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v: number) => metricAxisTick(v, metric)}
            width={60}
          />
          <Tooltip
            content={<ChartTooltip metric={metric} />}
            cursor={{ fill: "var(--muted)", fillOpacity: 0.4 }}
          />
          <Area
            type="monotone"
            dataKey="value"
            stroke={SERIES_COLORS[0]}
            fill="url(#usageArea)"
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function CategoryBarChart({
  rows,
  groupBy,
  metric,
}: {
  rows: UsageAggregate[];
  groupBy: UsageGroupBy;
  metric: Metric;
}) {
  const data = useMemo(() => {
    return [...rows]
      .map((r) => ({
        label: labelFor(r, groupBy),
        value: metricValue(r, metric),
      }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 20); // Cap to top 20 so the chart stays readable
  }, [rows, groupBy, metric]);

  return (
    <div className="h-72">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          layout="vertical"
          margin={{ top: 5, right: 24, left: 0, bottom: 0 }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis
            type="number"
            stroke="var(--muted-foreground)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v: number) => metricAxisTick(v, metric)}
          />
          <YAxis
            type="category"
            dataKey="label"
            stroke="var(--muted-foreground)"
            fontSize={11}
            width={160}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            content={<ChartTooltip metric={metric} />}
            cursor={{ fill: "var(--muted)", fillOpacity: 0.4 }}
          />
          <Legend wrapperStyle={{ display: "none" }} />
          <Bar dataKey="value" radius={[0, 4, 4, 0]}>
            {data.map((_, i) => (
              <Cell key={i} fill={SERIES_COLORS[i % SERIES_COLORS.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function ChartTooltip({
  active,
  payload,
  label,
  metric,
}: {
  active?: boolean;
  payload?: Array<{ value: number; payload: { label?: string } }>;
  label?: string;
  metric: Metric;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const item = payload[0];
  const resolvedLabel = label ?? item.payload.label ?? "";
  return (
    <div className="rounded-md border bg-background px-2.5 py-1.5 text-xs shadow-md">
      <div className="font-medium">{resolvedLabel}</div>
      <div className="text-muted-foreground tabular-nums">
        {formatMetric(item.value, metric)}
      </div>
    </div>
  );
}

// ── Results table ────────────────────────────────────────────────────

function ResultsTable({
  rows,
  groupBy,
}: {
  rows: UsageAggregate[];
  groupBy: UsageGroupBy;
}) {
  const sorted = useMemo(
    () => [...rows].sort((a, b) => b.cost_usd - a.cost_usd),
    [rows],
  );
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr className="border-b">
            <th className="text-left py-1.5 px-2 font-medium">
              {groupByLabel(groupBy)}
            </th>
            <th className="text-right py-1.5 px-2 font-medium">Rounds</th>
            <th className="text-right py-1.5 px-2 font-medium">Input</th>
            <th className="text-right py-1.5 px-2 font-medium">Output</th>
            <th className="text-right py-1.5 px-2 font-medium">Cache R</th>
            <th className="text-right py-1.5 px-2 font-medium">Cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/60">
          {sorted.map((r, i) => (
            <tr key={i} className={cn(i % 2 === 1 && "bg-muted/30")}>
              <td className="py-1.5 px-2 font-mono truncate max-w-[240px]">
                {labelFor(r, groupBy)}
              </td>
              <td className="py-1.5 px-2 text-right tabular-nums">{r.rounds}</td>
              <td className="py-1.5 px-2 text-right tabular-nums">
                {formatTokens(r.input_tokens)}
              </td>
              <td className="py-1.5 px-2 text-right tabular-nums">
                {formatTokens(r.output_tokens)}
              </td>
              <td className="py-1.5 px-2 text-right tabular-nums text-muted-foreground">
                {r.cache_read_tokens > 0
                  ? formatTokens(r.cache_read_tokens)
                  : "—"}
              </td>
              <td className="py-1.5 px-2 text-right tabular-nums font-medium">
                {formatCost(r.cost_usd)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────

function aggregateTotals(rows: UsageAggregate[]) {
  return rows.reduce(
    (acc, r) => ({
      rounds: acc.rounds + r.rounds,
      input_tokens: acc.input_tokens + r.input_tokens,
      output_tokens: acc.output_tokens + r.output_tokens,
      cache_creation_tokens:
        acc.cache_creation_tokens + r.cache_creation_tokens,
      cache_read_tokens: acc.cache_read_tokens + r.cache_read_tokens,
      cost_usd: acc.cost_usd + r.cost_usd,
    }),
    {
      rounds: 0,
      input_tokens: 0,
      output_tokens: 0,
      cache_creation_tokens: 0,
      cache_read_tokens: 0,
      cost_usd: 0,
    },
  );
}

function metricValue(row: UsageAggregate, metric: Metric): number {
  switch (metric) {
    case "cost_usd":
      return row.cost_usd;
    case "input_tokens":
      return row.input_tokens;
    case "output_tokens":
      return row.output_tokens;
    case "total_tokens":
      return row.input_tokens + row.output_tokens;
  }
}

function formatMetric(v: number, metric: Metric): string {
  if (metric === "cost_usd") return formatCost(v);
  return `${formatTokens(v)} tokens`;
}

function metricAxisTick(v: number, metric: Metric): string {
  if (metric === "cost_usd") return formatCost(v);
  return formatTokens(v);
}

function labelFor(row: UsageAggregate, groupBy: UsageGroupBy): string {
  if (groupBy === "user_name") {
    return (
      row.dimensions.user_name || row.dimensions.user_id || "(unknown)"
    );
  }
  const v = row.dimensions[groupBy];
  return v || "(none)";
}

function groupByLabel(groupBy: UsageGroupBy): string {
  return GROUP_BY_OPTIONS.find((o) => o.value === groupBy)?.label ?? groupBy;
}

function today(): string {
  return isoDay(new Date());
}

function isoDay(d: Date): string {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
