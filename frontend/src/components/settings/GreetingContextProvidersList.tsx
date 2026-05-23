/**
 * GreetingContextProvidersList — dynamic checkbox list of greeting context
 * providers, driven by the ``greeting.context_providers.list`` WS RPC.
 *
 * Replaces the raw ``enabled_context_providers`` array field in the
 * greeting service's settings card with a human-friendly toggle list.
 * Each row represents a discovered provider (Weather, Health, News
 * briefing, etc.); toggling one updates the ``enabled_context_providers``
 * config key via the section's ``onChange`` callback.
 *
 * The component initialises ``enabledIds`` from the providers' ``enabled``
 * flags (which the backend derives from the saved config).  When the
 * backend returns an empty providers list (no context provider services
 * are running), it renders a tasteful empty state rather than nothing.
 */

import { useCallback, useEffect, useState } from "react";
import { useWsApi } from "@/hooks/useWsApi";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";

interface Provider {
  id: string;
  label: string;
  enabled: boolean;
}

interface GreetingContextProvidersListProps {
  /** Called with the new ordered list of enabled provider IDs whenever
   *  the user flips a toggle.  Matches the ``onChange`` contract of
   *  ``ConfigField``:  ``(key, value) => void``.  */
  onChange: (key: string, value: string[] | null) => void;
  /** Current value from the merged config (may be ``null`` meaning
   *  "all providers enabled").  Used only for the initial render when
   *  the RPC hasn't returned yet — the RPC's ``enabled`` flags are the
   *  canonical source of truth once loaded. */
  currentValue: string[] | null | undefined;
}

export function GreetingContextProvidersList({
  onChange,
  currentValue,
}: GreetingContextProvidersListProps) {
  const api = useWsApi();
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // enabledIds tracks local toggle state.  Null means "all enabled"
  // (same semantics as the backend's None).
  const [enabledIds, setEnabledIds] = useState<Set<string> | null>(null);

  // ── Load providers on mount ──────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    api
      .listGreetingContextProviders()
      .then((list) => {
        if (cancelled) return;
        setProviders(list);
        // Initialise from the RPC's authoritative enabled flags.
        const allEnabled = list.every((p) => p.enabled);
        setEnabledIds(
          allEnabled ? null : new Set(list.filter((p) => p.enabled).map((p) => p.id)),
        );
      })
      .catch((err) => {
        if (cancelled) return;
        setError((err as Error)?.message ?? "Could not load providers");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Toggle handler ───────────────────────────────────────────────
  const handleToggle = useCallback(
    (id: string, checked: boolean) => {
      setEnabledIds((prev) => {
        // If currently "all enabled" (null), seed from the full list.
        const base =
          prev !== null ? new Set(prev) : new Set(providers?.map((p) => p.id) ?? []);
        if (checked) {
          base.add(id);
        } else {
          base.delete(id);
        }
        // If every provider is now checked, collapse back to null ("all").
        const allChecked =
          providers !== null && base.size === providers.length;
        const next = allChecked ? null : base;
        // Propagate to parent immediately so the section marks itself
        // dirty and the Save button activates.
        onChange(
          "enabled_context_providers",
          next === null ? null : Array.from(next),
        );
        return next;
      });
    },
    [providers, onChange],
  );

  // ── Render ───────────────────────────────────────────────────────
  if (error) {
    return (
      <p className="text-xs text-destructive">
        Could not load context providers: {error}
      </p>
    );
  }

  if (providers === null) {
    return <LoadingSpinner text="Loading context providers…" className="py-2" />;
  }

  if (providers.length === 0) {
    return (
      <p className="text-xs text-muted-foreground italic">
        No context-provider services are running. Install and enable a
        weather, health, or feed-briefing integration to see providers here.
      </p>
    );
  }

  // Determine each row's checked state.
  // - If enabledIds has been set (from the RPC or from a toggle), use it.
  //   null enabledIds means "all providers enabled".
  // - Fall back to currentValue (from the merged config) if the RPC hasn't
  //   returned yet — this keeps the initial paint correct in the rare case
  //   where the promise resolves asynchronously after the first render.
  const isChecked = (id: string): boolean => {
    if (enabledIds !== null) return enabledIds.has(id);
    if (currentValue === null || currentValue === undefined) return true;
    return currentValue.includes(id);
  };

  return (
    <div className="space-y-2">
      {providers.map((provider) => {
        const checked = isChecked(provider.id);
        return (
          <div key={provider.id} className="flex items-center gap-3">
            <Switch
              id={`ctx-provider-${provider.id}`}
              checked={checked}
              onCheckedChange={(v) => handleToggle(provider.id, v)}
            />
            <Label
              htmlFor={`ctx-provider-${provider.id}`}
              className={checked ? "" : "text-muted-foreground"}
            >
              {provider.label}
            </Label>
          </div>
        );
      })}
    </div>
  );
}
