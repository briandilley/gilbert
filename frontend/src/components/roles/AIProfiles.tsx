import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/layout/PageHeader";
import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { AIProfileEditorDialog } from "./AIProfileEditorDialog";

export function AIProfiles() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: api.listProfiles,
    enabled: connected,
  });

  // ``undefined`` = closed, ``null`` = create, a name = edit that profile.
  const [target, setTarget] = useState<string | null | undefined>(undefined);

  const deleteMutation = useMutation({
    mutationFn: api.deleteProfile,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["ai-profiles"] }),
  });

  function openNew() {
    setTarget(null);
  }

  function openEdit(profile: { name: string }) {
    setTarget(profile.name);
  }

  return (
    <div>
      <PageHeader
        eyebrow="SECURITY"
        title="AI profiles"
        description="Named bundles of AI-backend + tool-set + system prompt. Services request a profile by name; the resolver picks a concrete backend at call time."
        actions={
          <Button size="sm" onClick={openNew}>
            <PlusIcon />
            New profile
          </Button>
        }
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        {isLoading && <LoadingSpinner text="Loading profiles..." className="p-4" />}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {data?.profiles.map((profile) => (
          <Card key={profile.name}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <span className="flex-1">{profile.name}</span>
                <Badge variant="secondary" className="text-xs">
                  {profile.tool_mode}
                </Badge>
                {(profile.backend || profile.model) && (
                  <Badge variant="outline" className="text-xs">
                    {profile.model || profile.backend || "default"}
                  </Badge>
                )}
                <Button
                  variant="ghost"
                  size="icon-xs"
                  onClick={() => openEdit(profile)}
                >
                  <PencilIcon className="size-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="text-destructive"
                  onClick={() => deleteMutation.mutate(profile.name)}
                >
                  <Trash2Icon className="size-3" />
                </Button>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {profile.description && (
                <p className="text-muted-foreground">{profile.description}</p>
              )}
              {profile.tools.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {profile.tools.map((t) => (
                    <Badge key={t} variant="outline" className="text-[10px]">
                      {t}
                    </Badge>
                  ))}
                </div>
              )}
              {profile.assigned_calls.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {profile.assigned_calls.map((c) => (
                    <Badge key={c} variant="secondary" className="text-[10px]">
                      {c}
                    </Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      <AIProfileEditorDialog
        open={target !== undefined}
        profileName={target ?? null}
        onClose={() => setTarget(undefined)}
      />
      </div>
    </div>
  );
}
