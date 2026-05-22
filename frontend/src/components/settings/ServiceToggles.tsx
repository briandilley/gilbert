/**
 * ServiceToggles — flat list of on/off toggles for optional services.
 *
 * The ``_services`` pseudo-namespace exposes one boolean param per
 * toggleable service. State is held in SettingsContext alongside
 * every other section so the global StatusBar aggregates this one's
 * unsaved edits too.
 */

import { useMemo } from "react";
import {
  Card,
  CardContent,
  CardEyebrow,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { SaveIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { useSettingsSection } from "./SettingsContext";
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
  const svcSection = sections.find((s) => s.namespace === "_services");
  const state = useSettingsSection(svcSection?.namespace ?? "_services");

  const merged = useMemo(
    () => ({ ...(svcSection?.values ?? {}), ...state.dirty }),
    [svcSection?.values, state.dirty],
  );

  const dirtyCount = Object.keys(state.dirty).length;
  const hasChanges = dirtyCount > 0;

  if (!svcSection) return null;

  return (
    <Card>
      <CardHeader>
        <CardEyebrow>services</CardEyebrow>
        <CardTitle>Optional services</CardTitle>
      </CardHeader>
      <CardContent className="py-2">
        <ul className="divide-y divide-border">
          {svcSection.params.map((p) => {
            const checked = !!merged[p.key];
            return (
              <li
                key={p.key}
                className="flex items-center justify-between gap-3 py-2.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">{humanize(p.key)}</div>
                  {p.description ? (
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {p.description}
                    </p>
                  ) : null}
                </div>
                <Switch
                  checked={checked}
                  onCheckedChange={(v: boolean) => state.setField(p.key, v)}
                />
              </li>
            );
          })}
        </ul>
      </CardContent>
      <CardFooter className="justify-between">
        <div className="text-xs">
          {state.saveStatus ? (
            <span
              className={cn(
                "font-mono",
                state.saveStatus.ok
                  ? "text-success"
                  : "text-destructive",
              )}
            >
              {state.saveStatus.message}
            </span>
          ) : hasChanges ? (
            <span className="font-mono text-(--signal)">
              {dirtyCount} unsaved
            </span>
          ) : (
            <span className="text-muted-foreground">No changes.</span>
          )}
        </div>
        <Button
          size="sm"
          disabled={!hasChanges}
          onClick={() => state.save()}
        >
          <SaveIcon />
          Save
        </Button>
      </CardFooter>
    </Card>
  );
}
