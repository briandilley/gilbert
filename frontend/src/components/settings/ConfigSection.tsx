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
import { ChevronDownIcon, ChevronRightIcon, SaveIcon, RotateCcwIcon, ZapIcon, ExternalLinkIcon } from "lucide-react";
import type {
  ConfigSection as ConfigSectionType,
  ConfigParamMeta,
  ConfigActionMeta,
  ConfigActionResult,
} from "@/types/config";

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

  // No backend selector = multi-backend service (e.g., AI with
  // backends.anthropic.*, backends.openai.*).
  // Group by the second segment when keys start with "backends.",
  // otherwise by the first segment.
  const groups: { label: string; params: ConfigParamMeta[] }[] = [];
  const seen = new Set<string>();
  for (const p of params) {
    const parts = p.key.split(".");
    const isNested = parts[0] === "backends" && parts.length >= 3;
    const groupKey = isNested ? `${parts[0]}.${parts[1]}` : parts[0];
    const groupLabel = isNested ? parts[1] : groupKey;
    if (seen.has(groupKey)) continue;
    seen.add(groupKey);
    groups.push({
      label: humanize(groupLabel),
      params: params.filter((q) => q.key === groupKey || q.key.startsWith(`${groupKey}.`)),
    });
  }
  return groups;
}

interface ActionUIState {
  status: "idle" | "running" | "ok" | "error" | "pending";
  message: string;
  /** When set, the button becomes a "Continue" that invokes this key instead. */
  followup: string;
}

export function ConfigSection({ section }: ConfigSectionProps) {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const [expanded, setExpanded] = useState(false);
  const [localValues, setLocalValues] = useState<Record<string, unknown>>({});
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [actionStates, setActionStates] = useState<Record<string, ActionUIState>>({});

  // Merge defaults → server values → local edits so fields show their
  // declared default when no value has been stored yet.
  const merged = useMemo(() => {
    const defaults: Record<string, unknown> = {};
    for (const p of section.params) {
      if (p.default != null) defaults[p.key] = p.default;
    }
    return { ...defaults, ...section.values, ...localValues };
  }, [section.params, section.values, localValues]);

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

  const runAction = useCallback(
    async (action: ConfigActionMeta, keyOverride?: string) => {
      if (action.confirm && !keyOverride) {
        if (!window.confirm(action.confirm)) return;
      }
      const invokeKey = keyOverride ?? action.key;
      setActionStates((prev) => ({
        ...prev,
        [action.key]: { status: "running", message: "", followup: "" },
      }));
      try {
        const resp = await api.invokeConfigAction(section.namespace, invokeKey);
        const result: ConfigActionResult = resp.result;

        // If the backend asked us to persist values, push them into
        // localValues as if the user had typed them. This lets the user
        // see the new values as pending changes and click Save
        // explicitly — matching how every other field behaves and
        // activating the Save button without a manual focus/blur.
        const persistRaw = (result.data ?? {})["persist"];
        if (persistRaw && typeof persistRaw === "object") {
          const persist = persistRaw as Record<string, unknown>;
          if (Object.keys(persist).length > 0) {
            setLocalValues((prev) => ({ ...prev, ...persist }));
            setSaveStatus(null);
          }
        }

        setActionStates((prev) => ({
          ...prev,
          [action.key]: {
            status: result.status,
            message: result.message,
            followup: result.followup_action ?? "",
          },
        }));

        if (result.open_url) {
          window.open(result.open_url, "_blank", "noopener,noreferrer");
        }

        // Auto-clear ok messages after a few seconds; leave errors/pending up.
        // Actions that produced `persist` values stay visible longer because
        // the user still needs to click Save — clearing the "click Save to
        // store" message too fast is confusing.
        const hasPersist =
          persistRaw && typeof persistRaw === "object" &&
          Object.keys(persistRaw as Record<string, unknown>).length > 0;
        if (result.status === "ok") {
          setTimeout(() => {
            setActionStates((prev) => {
              const next = { ...prev };
              if (next[action.key]?.status === "ok") delete next[action.key];
              return next;
            });
          }, hasPersist ? 20000 : 5000);
        }
      } catch (exc) {
        setActionStates((prev) => ({
          ...prev,
          [action.key]: {
            status: "error",
            message: (exc as Error)?.message ?? String(exc),
            followup: "",
          },
        }));
      }
    },
    [api, section.namespace],
  );

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

          {/* Everything else only visible when enabled (or if no enabled toggle).
              Wrap in a soft-bordered container when there's an enable toggle so
              the dependency relationship is visually obvious — the toggle is
              the "root" and the container visually holds "the stuff it gates."
              Services without an enable toggle render the body bare since
              there's no conditional dependency to signal. Inner sections
              (per-backend groups below) use stronger borders so nested cards
              still read as distinct. */}
          {(!enabledParam || merged["enabled"] === true) && (
            <div
              className={
                enabledParam
                  ? "rounded-md border border-border/40 bg-muted/10 p-4"
                  : ""
              }
            >

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
            <div className={serviceParams.length > 0 || backendParam ? "mt-4 pt-4 border-t border-dashed" : ""}>
              {backendGroups(backendSettingsParams, backendName, !!backendParam).map((group) => {
                // Multi-backend groups collapse unless explicitly enabled.
                // An unset / ``null`` / ``undefined`` stored value counts as
                // disabled — otherwise every newly-registered backend would
                // expand its full config on first render (hiding lesser-used
                // ones behind a wall of api_key / model / base_url fields)
                // just because its declared default is ``True``.
                const enableParam = group.params.find((p) => p.key.endsWith(".enabled"));
                const isEnabled = enableParam
                  ? getValue(enableParam.key) === true
                  : true;
                const otherParams = enableParam
                  ? group.params.filter((p) => p !== enableParam)
                  : group.params;

                return (
                  <div
                    key={group.label}
                    className="mb-4 rounded-md border border-border/60 bg-muted/20 p-4 last:mb-0"
                  >
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                      {group.label}
                    </h4>
                    {/* Show enable toggle if present */}
                    {enableParam && (
                      <div className={isEnabled && otherParams.length > 0 ? "mb-3" : ""}>
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

            </div>
          )}

          {/* Actions — one-click operations declared by the service/backend.
              Filter by current dropdown backend so switching backends (even
              unsaved) immediately surfaces the right button set. Actions
              with an empty ``backend`` field are service-level and always
              render. */}
          {(() => {
            const currentBackendName = String(merged["backend"] ?? "");
            const visible = (section.actions ?? []).filter((a) =>
              !a.hidden && (!a.backend || a.backend === currentBackendName),
            );
            if (visible.length === 0) return null;
            const backendChangedUnsaved =
              backendParam !== undefined &&
              "backend" in localValues &&
              localValues["backend"] !== section.values["backend"];
            return (
            <div className="mt-6 pt-4 border-t border-dashed">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                Actions
              </h4>
              {backendChangedUnsaved && (
                <div className="text-xs text-amber-400 mb-3">
                  Save to enable actions for the new backend.
                </div>
              )}
              <div className="space-y-2">
                {visible.map((action) => {
                  const state = actionStates[action.key];
                  const running = state?.status === "running";
                  const pending = state?.status === "pending";
                  const isFollowup = pending && !!state?.followup;
                  const nextKey = isFollowup ? state.followup : action.key;
                  const label = isFollowup ? "Continue" : action.label;

                  return (
                    <div key={action.key} className="flex flex-col gap-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={running}
                          onClick={() => runAction(action, isFollowup ? nextKey : undefined)}
                        >
                          {isFollowup
                            ? <ExternalLinkIcon className="size-3.5 mr-1.5" />
                            : <ZapIcon className="size-3.5 mr-1.5" />}
                          {running ? "Running..." : label}
                        </Button>
                        {action.description && !state && (
                          <span className="text-xs text-muted-foreground">{action.description}</span>
                        )}
                        {state?.message && (
                          <span
                            className={
                              state.status === "error"
                                ? "text-xs text-red-400"
                                : state.status === "ok"
                                ? "text-xs text-green-400"
                                : state.status === "pending"
                                ? "text-xs text-amber-400"
                                : "text-xs text-muted-foreground"
                            }
                          >
                            {state.message}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
            );
          })()}

          {/* Save / Reset bar */}
          <div className="flex flex-wrap items-center gap-2 mt-6 pt-4 border-t">
            <Button size="sm" disabled={!hasChanges || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
              <SaveIcon className="size-3.5 mr-1.5" />
              {saveMutation.isPending ? "Saving..." : "Save"}
            </Button>
            <Button variant="outline" size="sm" disabled={resetMutation.isPending} onClick={() => resetMutation.mutate()}>
              <RotateCcwIcon className="size-3.5 mr-1.5" />
              Reset to Defaults
            </Button>
            {saveStatus && (
              <span className={`text-xs ${saveStatus.includes("fail") ? "text-red-400" : "text-green-400"}`}>
                {saveStatus}
              </span>
            )}
            {hasChanges && !saveStatus && (
              <span className="text-xs text-amber-400">Unsaved changes</span>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}
