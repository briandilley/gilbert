/**
 * ConfigSection — collapsible card for a single service namespace.
 *
 * Separates service-level params from backend-specific params (settings.*).
 * Backend params appear in a clearly labelled section below the backend selector.
 */

import { useState, useCallback, useMemo } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { ConfigField } from "./ConfigField";
import { ChevronDownIcon, ChevronRightIcon, SaveIcon, RotateCcwIcon } from "lucide-react";
import type { ConfigSection as ConfigSectionType, ConfigParamMeta } from "@/types/config";

interface ConfigSectionProps {
  section: ConfigSectionType;
}

function humanize(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Group backend params for display. */
function backendGroups(
  params: ConfigParamMeta[],
  singleBackendName: string,
  hasBackendSelector: boolean,
): { label: string; params: ConfigParamMeta[] }[] {
  // If the service has a backend selector, all backend params are for that
  // one backend — show them in a single group.
  if (hasBackendSelector) {
    const label = singleBackendName
      ? `${humanize(singleBackendName)} Settings`
      : "Backend Settings";
    return [{ label, params }];
  }

  // No backend selector = multi-backend service (e.g., knowledge).
  // Group by the first segment of the key (local.*, gdrive.*).
  const groups: { label: string; params: ConfigParamMeta[] }[] = [];
  const seen = new Set<string>();
  for (const p of params) {
    const prefix = p.key.split(".")[0];
    if (seen.has(prefix)) continue;
    seen.add(prefix);
    groups.push({
      label: `${humanize(prefix)} Backend`,
      params: params.filter((q) => q.key === prefix || q.key.startsWith(`${prefix}.`)),
    });
  }
  return groups;
}

export function ConfigSection({ section }: ConfigSectionProps) {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const [expanded, setExpanded] = useState(false);
  const [localValues, setLocalValues] = useState<Record<string, unknown>>({});
  const [saveStatus, setSaveStatus] = useState<string | null>(null);

  // Merge server values with local edits
  const merged = useMemo(
    () => ({ ...section.values, ...localValues }),
    [section.values, localValues],
  );

  const hasChanges = Object.keys(localValues).length > 0;

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    setLocalValues((prev) => ({ ...prev, [key]: value }));
    setSaveStatus(null);
  }, []);

  const saveMutation = useMutation({
    mutationFn: () => api.setConfigSection(section.namespace, localValues),
    onSuccess: (result) => {
      setLocalValues({});
      queryClient.invalidateQueries({ queryKey: ["config"] });
      const results = result?.results ?? {};
      const restarted = Object.values(results).some(
        (r: any) => r?.message?.includes("restarted") || r?.message?.includes("enabled"),
      );
      setSaveStatus(restarted ? "Saved — service restarting..." : "Saved");
      setTimeout(() => setSaveStatus(null), 3000);
    },
    onError: () => {
      setSaveStatus("Save failed");
      setTimeout(() => setSaveStatus(null), 3000);
    },
  });

  const resetMutation = useMutation({
    mutationFn: () => api.resetConfigSection(section.namespace),
    onSuccess: () => {
      setLocalValues({});
      queryClient.invalidateQueries({ queryKey: ["config"] });
      setSaveStatus("Reset to defaults");
      setTimeout(() => setSaveStatus(null), 3000);
    },
  });

  // Split params into groups
  const enabledParam = section.params.find((p) => p.key === "enabled");
  const backendParam = section.params.find((p) => p.key === "backend");
  const serviceParams = section.params.filter(
    (p) => p.key !== "enabled" && p.key !== "backend" && !p.backend_param,
  );
  const backendSettingsParams = section.params.filter((p) => p.backend_param);

  // Resolve the current backend name for display
  const backendName = String(merged["backend"] ?? "");

  /** Get the nested value for a dot-path key like "settings.api_key" */
  const getValue = (key: string): unknown => {
    // Check local edits first
    if (key in localValues) return localValues[key];
    // Navigate dot-path in server values
    const parts = key.split(".");
    let cur: any = section.values;
    for (const part of parts) {
      if (cur == null || typeof cur !== "object") return undefined;
      cur = cur[part];
    }
    return cur;
  };

  return (
    <Card className="overflow-hidden">
      {/* Header */}
      <button
        type="button"
        className="flex items-center w-full px-4 py-3 text-left hover:bg-muted/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded
          ? <ChevronDownIcon className="size-4 mr-2 shrink-0 text-muted-foreground" />
          : <ChevronRightIcon className="size-4 mr-2 shrink-0 text-muted-foreground" />
        }
        <span className="font-medium text-sm">{humanize(section.namespace)}</span>
        <div className="ml-auto flex items-center gap-2">
          {section.started && (
            <Badge variant="outline" className="text-[10px] text-green-500 border-green-500/40">running</Badge>
          )}
          {section.failed && (
            <Badge variant="outline" className="text-[10px] text-red-500 border-red-500/40">failed</Badge>
          )}
          {!section.started && !section.failed && !section.enabled && (
            <Badge variant="outline" className="text-[10px] text-muted-foreground">disabled</Badge>
          )}
        </div>
      </button>

      {/* Body */}
      {expanded && (
        <CardContent className="px-4 pb-4 pt-0">
          <Separator className="mb-4" />

          {/* Enabled toggle */}
          {enabledParam && (
            <div className="mb-4">
              <ConfigField param={enabledParam} value={merged["enabled"]} onChange={handleFieldChange} />
            </div>
          )}

          {/* Everything else only visible when enabled (or if no enabled toggle) */}
          {(!enabledParam || merged["enabled"] === true) && <>

          {/* Service-level params */}
          {serviceParams.length > 0 && (
            <div className="space-y-4 mb-4">
              {serviceParams.map((p) => (
                <ConfigField key={p.key} param={p} value={merged[p.key]} onChange={handleFieldChange} />
              ))}
            </div>
          )}

          {/* Backend selector — last service-level option, before backend settings */}
          {backendParam && (
            <div className="mb-4">
              <ConfigField param={backendParam} value={merged["backend"]} onChange={handleFieldChange} />
            </div>
          )}

          {/* Backend-specific settings — only shown when a backend is selected */}
          {backendSettingsParams.length > 0 && (!backendParam || backendName) && (
            <div className="mt-4 pt-4 border-t border-dashed">
              {backendGroups(backendSettingsParams, backendName, !!backendParam).map((group) => {
                // For multi-backend groups, check if this group's enabled toggle is on
                const enableParam = group.params.find((p) => p.key.endsWith(".enabled"));
                const isEnabled = enableParam ? getValue(enableParam.key) === true : true;
                const otherParams = enableParam
                  ? group.params.filter((p) => p !== enableParam)
                  : group.params;

                return (
                  <div key={group.label} className="mb-4">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                      {group.label}
                    </h4>
                    {/* Show enable toggle if present */}
                    {enableParam && (
                      <div className="mb-3">
                        <ConfigField param={enableParam} value={getValue(enableParam.key)} onChange={handleFieldChange} />
                      </div>
                    )}
                    {/* Only show other params if enabled */}
                    {isEnabled && otherParams.length > 0 && (
                      <div className="space-y-4">
                        {otherParams.map((p) => (
                          <ConfigField key={p.key} param={p} value={getValue(p.key)} onChange={handleFieldChange} />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          </>}

          {/* Save / Reset bar */}
          <div className="flex items-center gap-2 mt-6 pt-4 border-t">
            <Button size="sm" disabled={!hasChanges || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
              <SaveIcon className="size-3.5 mr-1.5" />
              {saveMutation.isPending ? "Saving..." : "Save"}
            </Button>
            <Button variant="outline" size="sm" disabled={resetMutation.isPending} onClick={() => resetMutation.mutate()}>
              <RotateCcwIcon className="size-3.5 mr-1.5" />
              Reset to Defaults
            </Button>
            {saveStatus && (
              <span className={`text-xs ml-2 ${saveStatus.includes("fail") ? "text-red-400" : "text-green-400"}`}>
                {saveStatus}
              </span>
            )}
            {hasChanges && !saveStatus && (
              <span className="text-xs text-amber-400 ml-2">Unsaved changes</span>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}
