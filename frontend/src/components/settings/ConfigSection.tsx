/**
 * ConfigSection — collapsible card for a single service namespace.
 *
 * Visual structure follows the design-system Card vocabulary. State
 * lives in SettingsContext so the page-level StatusBar can aggregate
 * dirty edits across every section and "Save all".
 */

import { useCallback, useMemo, useState } from "react";
import {
  Card,
  CardContent,
  CardEyebrow,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useWsApi } from "@/hooks/useWsApi";
import { ConfigField } from "./ConfigField";
import { GreetingContextProvidersList } from "./GreetingContextProvidersList";
import { useSettingsSection } from "./SettingsContext";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  ExternalLinkIcon,
  RotateCcwIcon,
  SaveIcon,
  ZapIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  ConfigSection as ConfigSectionType,
  ConfigParamMeta,
  ConfigActionMeta,
  ConfigActionResult,
} from "@/types/config";

interface ConfigSectionProps {
  section: ConfigSectionType;
  /** When non-null, force-expand this section if any of its params /
   *  namespace / description matches. Wired by the page's search box. */
  searchQuery?: string;
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
  merged: Record<string, unknown>,
): { label: string; params: ConfigParamMeta[] }[] {
  if (hasBackendSelector) {
    const label = singleBackendName
      ? `${humanize(singleBackendName)} backend`
      : "Backend";
    return [{ label, params }];
  }
  const groups: { label: string; params: ConfigParamMeta[] }[] = [];
  const seen = new Set<string>();
  for (const p of params) {
    const parts = p.key.split(".");
    // Two key layouts we group on:
    //   ``backends.<name>.<...>``           — single-role aggregator
    //                                         (e.g. tts, speaker)
    //   ``<role>.backends.<name>.<...>``    — multi-role aggregator
    //                                         (e.g. transcription, where
    //                                         <role> ∈ {batch, streaming,
    //                                         wake_word})
    let isNested = false;
    let groupKey = parts[0];
    let groupLabel = parts[0];
    let multiRole: { role: string; name: string } | null = null;
    if (parts[0] === "backends" && parts.length >= 3) {
      isNested = true;
      groupKey = `${parts[0]}.${parts[1]}`;
      groupLabel = parts[1];
    } else if (parts.length >= 4 && parts[1] === "backends") {
      isNested = true;
      groupKey = `${parts[0]}.${parts[1]}.${parts[2]}`;
      // Show "<role> · <backend>" so users can tell which role this
      // backend slot belongs to when one provider implements multiple
      // roles (e.g. ElevenLabs Scribe is both batch and streaming).
      groupLabel = `${humanize(parts[0])} · ${humanize(parts[2])}`;
      multiRole = { role: parts[0], name: parts[2] };
    }
    if (seen.has(groupKey)) continue;
    seen.add(groupKey);
    // Multi-role aggregators (transcription's batch/streaming/wake_word)
    // emit every registered backend's params. Hide every backend group
    // whose name doesn't match the currently selected ``<role>.default``
    // so users only see settings for the backend they actually picked.
    if (multiRole) {
      const selected = merged[`${multiRole.role}.default`];
      if (typeof selected === "string" && selected !== "" && selected !== multiRole.name) {
        continue;
      }
    }
    groups.push({
      label: isNested && groupLabel.includes(" · ") ? groupLabel : humanize(groupLabel),
      params: params.filter(
        (q) => q.key === groupKey || q.key.startsWith(`${groupKey}.`),
      ),
    });
  }
  return groups;
}

interface ActionUIState {
  status: "idle" | "running" | "ok" | "error" | "pending";
  message: string;
  followup: string;
}

export function ConfigSection({ section, searchQuery }: ConfigSectionProps) {
  const api = useWsApi();
  const sectionState = useSettingsSection(section.namespace);
  const localValues = sectionState.dirty;
  const saveStatus = sectionState.saveStatus;

  const [userExpanded, setUserExpanded] = useState(false);
  const [actionStates, setActionStates] = useState<Record<string, ActionUIState>>(
    {},
  );

  // Auto-expand when search matches this section.
  const searchMatches = useMemo(() => {
    if (!searchQuery) return false;
    const q = searchQuery.toLowerCase();
    if (section.namespace.toLowerCase().includes(q)) return true;
    return section.params.some(
      (p) =>
        p.key.toLowerCase().includes(q) ||
        (p.description ?? "").toLowerCase().includes(q),
    );
  }, [searchQuery, section.namespace, section.params]);
  const expanded = userExpanded || searchMatches;

  // Merge defaults → server values → local edits.
  const merged = useMemo(() => {
    const defaults: Record<string, unknown> = {};
    for (const p of section.params) {
      if (p.default != null) defaults[p.key] = p.default;
    }
    return { ...defaults, ...section.values, ...localValues };
  }, [section.params, section.values, localValues]);

  const hasChanges = Object.keys(localValues).length > 0;

  const handleFieldChange = useCallback(
    (key: string, value: unknown) => {
      sectionState.setField(key, value);
    },
    [sectionState],
  );

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
        const resp = await api.invokeConfigAction(section.namespace, invokeKey, {
          values: merged,
        });
        const result: ConfigActionResult = resp.result;

        const persistRaw = (result.data ?? {})["persist"];
        if (persistRaw && typeof persistRaw === "object") {
          const persist = persistRaw as Record<string, unknown>;
          if (Object.keys(persist).length > 0) {
            sectionState.setFields(persist);
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

        const hasPersist =
          persistRaw &&
          typeof persistRaw === "object" &&
          Object.keys(persistRaw as Record<string, unknown>).length > 0;
        if (result.status === "ok") {
          setTimeout(
            () => {
              setActionStates((prev) => {
                const next = { ...prev };
                if (next[action.key]?.status === "ok") delete next[action.key];
                return next;
              });
            },
            hasPersist ? 20000 : 5000,
          );
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
    [api, merged, section.namespace, sectionState],
  );

  // Split params into groups
  const enabledParam = section.params.find((p) => p.key === "enabled");
  const backendParam = section.params.find((p) => p.key === "backend");
  // For the greeting namespace, the enabled_context_providers array field is
  // replaced by GreetingContextProvidersList (a dynamic checkbox driven by
  // the greeting.context_providers.list RPC). Exclude it from the generic loop.
  const isGreetingSection = section.namespace === "greeting";
  const serviceParams = section.params.filter(
    (p) =>
      p.key !== "enabled" &&
      p.key !== "backend" &&
      !p.backend_param &&
      !(isGreetingSection && p.key === "enabled_context_providers"),
  );
  const backendSettingsParams = section.params.filter((p) => p.backend_param);

  const backendName = String(merged["backend"] ?? "");

  /** Get the nested value for a dot-path key. Falls back to the
   *  param's declared default. */
  const getValue = (key: string): unknown => {
    if (key in localValues) return localValues[key];
    const parts = key.split(".");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let cur: any = section.values;
    for (const part of parts) {
      if (cur == null || typeof cur !== "object") {
        cur = undefined;
        break;
      }
      cur = cur[part];
    }
    if (cur === undefined) {
      const param = section.params.find((p) => p.key === key);
      return param?.default ?? undefined;
    }
    return cur;
  };

  // ── Status pill ────────────────────────────────────────────────
  const statusPill = (() => {
    if (section.started) return <Badge variant="active" dot>running</Badge>;
    if (section.failed) return <Badge variant="error" dot>failed</Badge>;
    if (!section.enabled) return <Badge variant="off" dot>off</Badge>;
    return null;
  })();

  const sectionEnabled = !enabledParam || merged["enabled"] === true;

  return (
    <Card>
      <button
        type="button"
        onClick={() => setUserExpanded((v) => !v)}
        className={cn(
          "group/header w-full text-left",
          "transition-colors duration-(--duration-fast) ease-(--ease-out)",
          "hover:bg-foreground/[0.025]",
          expanded && "border-b border-border",
        )}
      >
        <CardHeader className="grid-cols-[auto_1fr_auto] items-center py-3 gap-x-2.5">
          {expanded ? (
            <ChevronDownIcon className="size-3.5 text-muted-foreground row-span-2" />
          ) : (
            <ChevronRightIcon className="size-3.5 text-muted-foreground row-span-2" />
          )}
          <div className="min-w-0">
            <CardEyebrow>{section.namespace}</CardEyebrow>
            <CardTitle className="mt-1 truncate">
              {humanize(section.namespace)}
            </CardTitle>
          </div>
          {statusPill ? (
            <div className="row-span-2 self-center">{statusPill}</div>
          ) : null}
        </CardHeader>
      </button>

      {expanded && (
        <CardContent className="py-4 space-y-4">
          {enabledParam && (
            <ConfigField
              param={enabledParam}
              value={merged["enabled"]}
              onChange={handleFieldChange}
              namespace={section.namespace}
            />
          )}

          {sectionEnabled && (
            <>
              {serviceParams.length > 0 && (
                <div className="space-y-4">
                  {serviceParams.map((p) => (
                    <ConfigField
                      key={p.key}
                      param={p}
                      value={merged[p.key]}
                      onChange={handleFieldChange}
                      namespace={section.namespace}
                    />
                  ))}
                </div>
              )}

              {isGreetingSection && (
                <Card size="sm">
                  <CardHeader className="pb-1">
                    <CardTitle className="text-sm">Context contributors</CardTitle>
                    <p className="text-xs text-muted-foreground mt-1">
                      Choose which services contribute to the greeting's
                      available_context block. Each service decides what facts to
                      expose; you can write rules in the greeting prompt template
                      for how the AI should use them.
                    </p>
                  </CardHeader>
                  <CardContent className="pt-2">
                    <GreetingContextProvidersList
                      onChange={handleFieldChange}
                      currentValue={merged["enabled_context_providers"] as string[] | null | undefined}
                    />
                  </CardContent>
                </Card>
              )}

              {backendParam && (
                <ConfigField
                  param={backendParam}
                  value={merged["backend"]}
                  onChange={handleFieldChange}
                  namespace={section.namespace}
                />
              )}

              {backendSettingsParams.length > 0 &&
                (!backendParam || backendName) &&
                backendGroups(
                  backendSettingsParams,
                  backendName,
                  !!backendParam,
                  merged,
                ).map((group) => {
                  const enableParam = group.params.find((p) =>
                    p.key.endsWith(".enabled"),
                  );
                  const isEnabled = enableParam
                    ? getValue(enableParam.key) === true
                    : true;
                  const otherParams = enableParam
                    ? group.params.filter((p) => p !== enableParam)
                    : group.params;

                  return (
                    <Card key={group.label} size="sm">
                      <CardHeader className="pb-1">
                        <CardEyebrow>{group.label.toLowerCase()}</CardEyebrow>
                      </CardHeader>
                      <CardContent className="space-y-3">
                        {enableParam && (
                          <ConfigField
                            param={enableParam}
                            value={getValue(enableParam.key)}
                            onChange={handleFieldChange}
                            namespace={section.namespace}
                          />
                        )}
                        {isEnabled &&
                          otherParams.map((p) => (
                            <ConfigField
                              key={p.key}
                              param={p}
                              value={getValue(p.key)}
                              onChange={handleFieldChange}
                              namespace={section.namespace}
                            />
                          ))}
                      </CardContent>
                    </Card>
                  );
                })}
            </>
          )}

          <ActionsBlock
            actions={section.actions ?? []}
            actionStates={actionStates}
            runAction={runAction}
            backendName={String(merged["backend"] ?? "")}
            hasBackendChangeUnsaved={
              backendParam !== undefined &&
              "backend" in localValues &&
              localValues["backend"] !== section.values["backend"]
            }
            hasChanges={hasChanges}
          />
        </CardContent>
      )}

      {expanded && (
        <CardFooter className="justify-between">
          <div className="text-xs">
            {saveStatus ? (
              <span
                className={cn(
                  "font-mono",
                  saveStatus.ok ? "text-success" : "text-destructive",
                )}
              >
                {saveStatus.message}
              </span>
            ) : hasChanges ? (
              <span className="font-mono text-(--signal)">
                {Object.keys(localValues).length} unsaved change
                {Object.keys(localValues).length === 1 ? "" : "s"}
              </span>
            ) : (
              <span className="text-muted-foreground">No changes.</span>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="sm"
              onClick={() => sectionState.resetToDefaults()}
            >
              <RotateCcwIcon />
              Reset
            </Button>
            <Button
              size="sm"
              disabled={!hasChanges}
              onClick={() => sectionState.save()}
            >
              <SaveIcon />
              Save
            </Button>
          </div>
        </CardFooter>
      )}
    </Card>
  );
}

// ──────────────────────────────────────────────────────────────────
// Actions block — extracted for readability. Same behavior.
// ──────────────────────────────────────────────────────────────────

interface ActionsBlockProps {
  actions: ConfigActionMeta[];
  actionStates: Record<string, ActionUIState>;
  runAction: (action: ConfigActionMeta, keyOverride?: string) => Promise<void>;
  backendName: string;
  hasBackendChangeUnsaved: boolean;
  hasChanges: boolean;
}

function ActionsBlock({
  actions,
  actionStates,
  runAction,
  backendName,
  hasBackendChangeUnsaved,
  hasChanges,
}: ActionsBlockProps) {
  const visible = actions.filter(
    (a) => !a.hidden && (!a.backend || !backendName || a.backend === backendName),
  );
  if (visible.length === 0) return null;

  return (
    <div className="pt-1">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
          actions
        </span>
      </div>

      {hasBackendChangeUnsaved ? (
        <p className="mb-2 text-xs text-warning">
          Save to enable actions for the new backend.
        </p>
      ) : hasChanges ? (
        <p className="mb-2 text-xs text-warning">
          Unsaved changes — actions run against the saved values, not
          the ones you just edited. Save first.
        </p>
      ) : null}

      <div className="space-y-1">
        {visible.map((action) => {
          const state = actionStates[action.key];
          const running = state?.status === "running";
          const pending = state?.status === "pending";
          const isFollowup = pending && !!state?.followup;
          const nextKey = isFollowup ? state.followup : action.key;
          const label = isFollowup ? "Continue" : action.label;
          const statusColor =
            state?.status === "error"
              ? "text-destructive"
              : state?.status === "ok"
                ? "text-success"
                : state?.status === "pending"
                  ? "text-warning"
                  : "text-muted-foreground";

          return (
            <div
              key={action.key}
              className="flex flex-wrap items-center gap-2"
            >
              <Button
                size="sm"
                variant="outline"
                disabled={running}
                onClick={() =>
                  runAction(action, isFollowup ? nextKey : undefined)
                }
              >
                {isFollowup ? <ExternalLinkIcon /> : <ZapIcon />}
                {running ? "Running…" : label}
              </Button>
              {action.description && !state && (
                <span className="text-xs text-muted-foreground">
                  {action.description}
                </span>
              )}
              {state?.message && (
                <span className={cn("text-xs font-mono", statusColor)}>
                  {state.message}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
