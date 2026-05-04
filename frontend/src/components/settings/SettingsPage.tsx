/**
 * SettingsPage — admin-only configuration management.
 *
 * Category selection is synced to the URL search params so browser
 * history/back button works.
 */

import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ConfigSection } from "./ConfigSection";
import { ServiceToggles } from "./ServiceToggles";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import type { ConfigCategory } from "@/types/config";

export function SettingsPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [searchParams, setSearchParams] = useSearchParams();

  const { data, isLoading } = useQuery({
    queryKey: ["config"],
    queryFn: api.describeConfig,
    enabled: connected,
    refetchInterval: 30_000,
  });

  const categories: ConfigCategory[] = data?.categories ?? [];
  const activeCategory = searchParams.get("category") || "";

  // Auto-select first category if none in URL
  useEffect(() => {
    if (!activeCategory && categories.length > 0) {
      setSearchParams({ category: categories[0].name }, { replace: true });
    }
  }, [categories, activeCategory, setSearchParams]);

  const setCategory = (name: string) => {
    setSearchParams({ category: name });
  };

  const current = categories.find((c) => c.name === activeCategory);

  if (isLoading) {
    return <LoadingSpinner text="Loading configuration..." className="p-8" />;
  }

  if (categories.length === 0) {
    return (
      <div className="p-4 sm:p-6 max-w-4xl mx-auto text-center text-muted-foreground">
        No configurable services found.
      </div>
    );
  }

  return (
    <div className="p-4 sm:p-6 space-y-4 sm:space-y-6 max-w-4xl mx-auto">
      <h1 className="text-xl sm:text-2xl font-semibold text-center">Settings</h1>

      {/* Category selector */}
      <div className="flex justify-center">
        <Select value={activeCategory} onValueChange={(v) => { if (v) setCategory(v); }}>
          <SelectTrigger className="w-full max-w-xs sm:w-64">
            <SelectValue placeholder="Select category..." />
          </SelectTrigger>
          <SelectContent>
            {categories.map((cat) => (
              <SelectItem key={cat.name} value={cat.name}>
                {cat.name}
                <span className="ml-1.5 text-xs text-muted-foreground">
                  ({cat.sections.length})
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Service sections for active category */}
      {current && current.name === "Services" ? (
        <ServiceToggles sections={current.sections} />
      ) : current && (
        <div className="space-y-3">
          {current.sections.map((section) => (
            <ConfigSection key={section.namespace} section={section} />
          ))}
          {/* Plugins can contribute admin-scoped panels to a category
              via a "settings.<category>" slot — e.g. a future plugin
              could mount its own management UI under its own
              category. The slot only renders panels whose plugin
              declared required_role="admin", filtered server-side. */}
          <PluginPanelSlot
            slot={`settings.${current.name.toLowerCase()}`}
          />
        </div>
      )}
    </div>
  );
}
