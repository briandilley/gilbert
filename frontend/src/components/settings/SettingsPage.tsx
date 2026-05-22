/**
 * SettingsPage — admin-only configuration management.
 *
 * Layout:
 *   - PageHeader across the top.
 *   - Global StatusBar (sticky below the header) — aggregates dirty
 *     edits across every section and surfaces "Save all" / "Discard".
 *   - Below: left rail of categories (desktop) / Select dropdown
 *     (mobile) + a scrollable content pane showing the active
 *     category's sections.
 *
 * State for every ConfigSection lives in SettingsProvider so the
 * StatusBar can aggregate. Category selection + search query are
 * synced to the URL search params so back-button works.
 */

import { useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { PageHeader } from "@/components/layout/PageHeader";
import { StatusBar } from "@/components/layout/StatusBar";
import { ConfigSection } from "./ConfigSection";
import { ServiceToggles } from "./ServiceToggles";
import {
  SettingsProvider,
  useSettingsApi,
  useSettingsAggregate,
} from "./SettingsContext";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { cn } from "@/lib/utils";
import { SearchIcon, SaveIcon, RotateCcwIcon, XIcon } from "lucide-react";
import type { ConfigCategory } from "@/types/config";

/** Does any param / namespace / description in this category match
 *  the search query? Used to filter the rail and the content pane. */
function categoryMatchesQuery(cat: ConfigCategory, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  if (cat.name.toLowerCase().includes(needle)) return true;
  return cat.sections.some(
    (s) =>
      s.namespace.toLowerCase().includes(needle) ||
      s.params.some(
        (p) =>
          p.key.toLowerCase().includes(needle) ||
          (p.description ?? "").toLowerCase().includes(needle),
      ),
  );
}

export function SettingsPage() {
  return (
    <SettingsProvider>
      <SettingsPageInner />
    </SettingsProvider>
  );
}

function SettingsPageInner() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [searchParams, setSearchParams] = useSearchParams();
  const settingsApi = useSettingsApi();
  const aggregate = useSettingsAggregate();

  const { data, isLoading } = useQuery({
    queryKey: ["config"],
    queryFn: api.describeConfig,
    enabled: connected,
    refetchInterval: 30_000,
  });

  const allCategories: ConfigCategory[] = data?.categories ?? [];
  const activeCategory = searchParams.get("category") || "";
  const query = searchParams.get("q") || "";

  // Apply search filter to the rail.
  const visibleCategories = allCategories.filter((c) =>
    categoryMatchesQuery(c, query),
  );

  useEffect(() => {
    // Auto-select the first VISIBLE category if the URL one is
    // either missing or filtered out by the search query.
    if (
      visibleCategories.length > 0 &&
      !visibleCategories.find((c) => c.name === activeCategory)
    ) {
      const next = { ...Object.fromEntries(searchParams), category: visibleCategories[0].name };
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleCategories, activeCategory]);

  const setCategory = (name: string) => {
    const next = { ...Object.fromEntries(searchParams), category: name };
    setSearchParams(next);
  };

  const setQuery = (next: string) => {
    const params = Object.fromEntries(searchParams);
    if (next) params.q = next;
    else delete params.q;
    setSearchParams(params, { replace: true });
  };

  const current = allCategories.find((c) => c.name === activeCategory);
  const totalSections = allCategories.reduce(
    (acc, c) => acc + c.sections.length,
    0,
  );

  if (isLoading) {
    return (
      <div>
        <PageHeader eyebrow="ADMIN" title="Settings" />
        <LoadingSpinner text="Loading configuration..." className="p-8" />
      </div>
    );
  }

  if (allCategories.length === 0) {
    return (
      <div>
        <PageHeader eyebrow="ADMIN" title="Settings" />
        <div className="px-6 py-12 text-center">
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
            No configurable services
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            Nothing to configure yet. Services declare their config
            params when they register.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="ADMIN"
        title="Settings"
        description={
          <>
            {allCategories.length} categor
            {allCategories.length === 1 ? "y" : "ies"}, {totalSections}{" "}
            service{totalSections === 1 ? "" : "s"}.
          </>
        }
      />

      {/* Global save bar — aggregates every section's dirty edits.
          Only renders when there's something to save, so the chrome
          doesn't compete with the section's own footer when there's
          nothing pending. */}
      {aggregate.isDirty && (
        <StatusBar
          tone="dirty"
          status={
            <span className="font-mono">
              <span className="text-(--signal)">{aggregate.totalFields}</span>{" "}
              unsaved change{aggregate.totalFields === 1 ? "" : "s"} across{" "}
              <span className="text-(--signal)">
                {aggregate.dirtyNamespaces.length}
              </span>{" "}
              service{aggregate.dirtyNamespaces.length === 1 ? "" : "s"}
            </span>
          }
          actions={
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={() => settingsApi.discardAll()}
              >
                <RotateCcwIcon />
                Discard
              </Button>
              <Button size="sm" onClick={() => settingsApi.saveAll()}>
                <SaveIcon />
                Save all
              </Button>
            </>
          }
        />
      )}

      {/* Mobile category select — below md there's no room for the rail. */}
      <div className="border-b border-border px-4 py-2 md:hidden">
        <Select
          value={activeCategory}
          onValueChange={(v) => {
            if (v) setCategory(v);
          }}
        >
          <SelectTrigger className="w-full">
            <SelectValue placeholder="Select category..." />
          </SelectTrigger>
          <SelectContent>
            {visibleCategories.map((cat) => (
              <SelectItem key={cat.name} value={cat.name}>
                {cat.name}
                <span className="ml-2 font-mono text-[10px] text-muted-foreground">
                  {cat.sections.length}
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-1 min-h-0">
        <aside className="hidden md:flex w-56 shrink-0 flex-col border-r border-border">
          {/* Search input pinned to the top of the rail. */}
          <div className="border-b border-border p-2">
            <div className="relative">
              <SearchIcon className="absolute left-2 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground pointer-events-none" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search settings..."
                className="pl-7 pr-7 h-7"
              />
              {query && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="absolute right-0.5 top-1/2 -translate-y-1/2"
                  onClick={() => setQuery("")}
                  title="Clear search"
                >
                  <XIcon />
                </Button>
              )}
            </div>
          </div>

          <nav className="flex flex-col gap-px p-2 overflow-y-auto">
            {visibleCategories.length === 0 ? (
              <div className="px-2 py-6 text-center">
                <p className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-muted-foreground">
                  No matches
                </p>
              </div>
            ) : (
              visibleCategories.map((cat) => {
                const active = cat.name === activeCategory;
                return (
                  <Link
                    key={cat.name}
                    to={{
                      search: (() => {
                        const params = new URLSearchParams(searchParams);
                        params.set("category", cat.name);
                        return `?${params.toString()}`;
                      })(),
                    }}
                    className={cn(
                      "group relative flex items-center justify-between gap-2",
                      "h-8 px-2.5 rounded-md text-sm leading-none",
                      "transition-[background-color,color] duration-(--duration-fast) ease-(--ease-out)",
                      active
                        ? "bg-foreground/8 text-foreground font-medium"
                        : "text-foreground/75 hover:bg-foreground/5 hover:text-foreground",
                    )}
                  >
                    {active && (
                      <span
                        aria-hidden
                        className="absolute left-0 top-1.5 bottom-1.5 w-[2px] rounded-r-full bg-(--signal)"
                      />
                    )}
                    <span className="truncate">{cat.name}</span>
                    <span className="font-mono text-[10.5px] text-muted-foreground">
                      {cat.sections.length}
                    </span>
                  </Link>
                );
              })
            )}
          </nav>
        </aside>

        <main className="flex-1 min-w-0 overflow-y-auto">
          <div className="px-4 py-4 md:px-6 md:py-6 space-y-3">
            {current && current.name === "Services" ? (
              <ServiceToggles sections={current.sections} />
            ) : current ? (
              <>
                {current.sections.map((section) => (
                  <ConfigSection
                    key={section.namespace}
                    section={section}
                    searchQuery={query}
                  />
                ))}
                <PluginPanelSlot
                  slot={`settings.${current.name.toLowerCase()}`}
                />
              </>
            ) : null}
          </div>
        </main>
      </div>
    </div>
  );
}
