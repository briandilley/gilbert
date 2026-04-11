/**
 * ServiceToggles — flat list of on/off toggles for optional services.
 *
 * Renders as a single card with one row per toggleable service.
 * No expand/collapse — just switches.
 */

import { useState, useMemo } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { SaveIcon } from "lucide-react";
import type { ConfigSection } from "@/types/config";

interface Props {
  sections: ConfigSection[];
}

function humanize(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function ServiceToggles({ sections }: Props) {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const [localValues, setLocalValues] = useState<Record<string, boolean>>({});
  const [saveStatus, setSaveStatus] = useState<string | null>(null);

  // The _services section has one boolean param per toggleable service
  const svcSection = sections.find((s) => s.namespace === "_services");
  if (!svcSection) return null;

  const merged = useMemo(
    () => ({ ...svcSection.values, ...localValues }),
    [svcSection.values, localValues],
  );

  const hasChanges = Object.keys(localValues).length > 0;

  const saveMutation = useMutation({
    mutationFn: () => api.setConfigSection(svcSection.namespace, localValues),
    onSuccess: () => {
      setLocalValues({});
      queryClient.invalidateQueries({ queryKey: ["config"] });
      setSaveStatus("Saved — services restarting...");
      setTimeout(() => setSaveStatus(null), 3000);
    },
    onError: () => {
      setSaveStatus("Save failed");
      setTimeout(() => setSaveStatus(null), 3000);
    },
  });

  return (
    <Card>
      <CardContent className="p-4 space-y-1">
        {svcSection.params.map((p) => {
          const checked = !!merged[p.key];
          return (
            <div key={p.key} className="flex items-center justify-between py-2">
              <div>
                <span className="text-sm font-medium">{humanize(p.key)}</span>
                <p className="text-xs text-muted-foreground">{p.description}</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={checked}
                className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${checked ? "bg-primary" : "bg-muted"}`}
                onClick={() => {
                  setLocalValues((prev) => ({ ...prev, [p.key]: !checked }));
                  setSaveStatus(null);
                }}
              >
                <span
                  className={`pointer-events-none block h-5 w-5 rounded-full bg-background shadow-lg ring-0 transition-transform ${checked ? "translate-x-5" : "translate-x-0"}`}
                />
              </button>
            </div>
          );
        })}

        <div className="flex items-center gap-2 pt-3 border-t">
          <Button size="sm" disabled={!hasChanges || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            <SaveIcon className="size-3.5 mr-1.5" />
            {saveMutation.isPending ? "Saving..." : "Save"}
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
    </Card>
  );
}
