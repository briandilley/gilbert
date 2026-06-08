/**
 * ServiceToggles — flat list of on/off toggles for optional services.
 *
 * The ``_services`` pseudo-namespace exposes one boolean param per
 * toggleable service. State is held in SettingsContext alongside
 * every other section so the global StatusBar aggregates this one's
 * unsaved edits too.
 */

import { useMemo, useState } from "react";
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

  // toggle-key -> reason for services blocked by an unmet enablement
  // dependency (ADR-0018). Backend ships this on the ``_services`` section.
  const disabledServices = svcSection?.disabled_services ?? {};

  // Lightweight inline "toast": when an admin tries to enable a service
  // whose prerequisite is unmet, surface the reason transiently. (Gilbert
  // has no toast library yet; an inline notice mirrors the existing
  // save-status notice pattern in this card without adding a dependency.)
  const [toggleNotice, setToggleNotice] = useState<string | null>(null);

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
            const disabledReason = disabledServices[p.key];
            return (
              <li
                key={p.key}
                className="flex items-center justify-between gap-3 py-2.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{humanize(p.key)}</span>
                    {disabledReason ? (
                      <Badge variant="warning" title={disabledReason}>
                        disabled
                      </Badge>
                    ) : null}
                  </div>
                  {disabledReason ? (
                    <p className="text-xs text-warning mt-0.5">{disabledReason}</p>
                  ) : p.description ? (
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {p.description}
                    </p>
                  ) : null}
                </div>
                <Switch
                  checked={checked}
                  onCheckedChange={(v: boolean) => {
                    // Surface the unmet-prerequisite reason when an admin
                    // turns one of these on; still record the edit so the
                    // intent is saved (the backend leaves it disabled until
                    // the prerequisite is enabled — never auto-enabled).
                    if (v && disabledReason) {
                      setToggleNotice(disabledReason);
                    } else {
                      setToggleNotice(null);
                    }
                    state.setField(p.key, v);
                  }}
                />
              </li>
            );
          })}
        </ul>
      </CardContent>
      <CardFooter className="justify-between">
        <div className="text-xs">
          {toggleNotice ? (
            <span className="font-mono text-warning">{toggleNotice}</span>
          ) : state.saveStatus ? (
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
