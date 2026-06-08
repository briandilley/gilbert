/**
 * Per-model config editor (ADR-0019).
 *
 * Admin-only. Lists every model from the enabled AI backends with its
 * stored ModelConfig — an `enabled` toggle plus generation defaults
 * (`temperature`, `max_tokens`, `context_window`). Edits are persisted
 * through the `ai.model_config.set` RPC, backed by `PerModelConfigProvider`
 * on the AI service. A blank numeric field means "unset" → the layered
 * resolver (backend ← per-model ← profile ← call) falls through to the
 * backend's own default.
 *
 * `context_window` is seeded from GGUF metadata at pull time by the local
 * model manager and only honoured by local runtimes (Ollama maps it to
 * `num_ctx`); hosted backends ignore it.
 */

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { PageHeader } from "@/components/layout/PageHeader";
import type { ModelConfigEntry, ModelConfigInput } from "@/types/chat";

function toNumberOrNull(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

export function ModelConfigEditor() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data, isLoading } = useQuery({
    queryKey: ["ai-model-configs"],
    queryFn: api.listModelConfigs,
    enabled: connected,
  });

  const saveMutation = useMutation({
    mutationFn: (cfg: ModelConfigInput) => api.setModelConfig(cfg),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["ai-model-configs"] }),
  });

  function save(backend: string, m: ModelConfigEntry, patch: Partial<ModelConfigEntry>) {
    const next = { ...m, ...patch };
    saveMutation.mutate({
      backend,
      model: next.id,
      enabled: next.enabled,
      temperature: next.temperature,
      max_tokens: next.max_tokens,
      context_window: next.context_window,
    });
  }

  return (
    <div>
      <PageHeader
        eyebrow="INTELLIGENCE"
        title="Per-model config"
        description="Generation defaults and an enabled toggle for each model. Values resolve in layers — backend default, then per-model, then profile, then call. A blank field means 'use the backend default'. Context window is honoured only by local runtimes (Ollama)."
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6 space-y-4">
        {isLoading && (
          <LoadingSpinner text="Loading models..." className="p-4" />
        )}
        {!isLoading && (data?.backends?.length ?? 0) === 0 && (
          <p className="text-sm text-muted-foreground">
            No AI backends are enabled. Enable a backend in Settings to
            configure its models here.
          </p>
        )}
        {data?.backends.map((backend) => (
          <Card key={backend.name}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">
                {backend.name.charAt(0).toUpperCase() + backend.name.slice(1)}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {backend.models.length === 0 && (
                <p className="text-xs text-muted-foreground">
                  This backend advertises no selectable models.
                </p>
              )}
              {backend.models.map((m) => (
                <div
                  key={m.id}
                  className="flex flex-wrap items-end gap-3 border-b pb-3 last:border-b-0 last:pb-0"
                >
                  <div className="min-w-40 flex-1">
                    <div className="text-sm font-medium">{m.name}</div>
                    <div className="text-xs text-muted-foreground">{m.id}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Label className="text-xs">Enabled</Label>
                    <Switch
                      checked={m.enabled}
                      onCheckedChange={(v: boolean) =>
                        save(backend.name, m, { enabled: v })
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs">Temperature</Label>
                    <Input
                      type="number"
                      step="0.1"
                      min="0"
                      max="2"
                      className="h-7 w-24 text-xs"
                      defaultValue={m.temperature ?? ""}
                      placeholder="default"
                      onBlur={(e) =>
                        save(backend.name, m, {
                          temperature: toNumberOrNull(e.target.value),
                        })
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs">Max tokens</Label>
                    <Input
                      type="number"
                      step="1"
                      min="1"
                      className="h-7 w-28 text-xs"
                      defaultValue={m.max_tokens ?? ""}
                      placeholder="default"
                      onBlur={(e) =>
                        save(backend.name, m, {
                          max_tokens: toNumberOrNull(e.target.value),
                        })
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <Label
                      className="text-xs"
                      title="Total token window the model loads with. Only local runtimes (Ollama) honour this — it sets num_ctx so large prompts don't get rejected. Blank = the runtime's own default."
                    >
                      Context window
                    </Label>
                    <Input
                      type="number"
                      step="1"
                      min="1"
                      className="h-7 w-28 text-xs"
                      defaultValue={m.context_window ?? ""}
                      placeholder="default"
                      onBlur={(e) =>
                        save(backend.name, m, {
                          context_window: toNumberOrNull(e.target.value),
                        })
                      }
                    />
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
