import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PackagePlusIcon, AlertCircleIcon } from "lucide-react";
import { PluginCard } from "./PluginCard";
import { PageHeader } from "@/components/layout/PageHeader";

export function PluginsPage() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();

  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  const { data: plugins, isLoading } = useQuery({
    queryKey: ["plugins"],
    queryFn: api.listPlugins,
    enabled: connected,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["plugins"] });

  const installMutation = useMutation({
    mutationFn: (input: { url: string; force: boolean }) =>
      api.installPlugin(input.url, input.force),
    onSuccess: () => {
      setUrl("");
      setError(null);
      invalidate();
    },
    onError: (err: unknown) => {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    },
  });

  const uninstallMutation = useMutation({
    mutationFn: api.uninstallPlugin,
    onSuccess: invalidate,
    onError: (err: unknown) => {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    },
  });

  function handleInstall(force: boolean = false) {
    const trimmed = url.trim();
    if (!trimmed) {
      setError("Please enter a plugin URL.");
      return;
    }
    setError(null);
    installMutation.mutate({ url: trimmed, force });
  }

  const sorted = [...(plugins ?? [])].sort((a, b) =>
    a.name.localeCompare(b.name),
  );

  return (
    <div>
      <PageHeader
        eyebrow="EXTENSIONS"
        title="Plugins"
        description={`${sorted.length} installed.`}
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6 space-y-3">
        {isLoading && <LoadingSpinner text="Loading plugins..." className="p-4" />}

      {/* Install form */}
      <Card>
        <CardContent className="pt-4 space-y-3">
          <div className="text-sm font-medium flex items-center gap-2">
            <PackagePlusIcon className="h-4 w-4" />
            Install a plugin
          </div>
          <p className="text-xs text-muted-foreground">
            Paste a GitHub URL (whole repo, or <code>/tree/&lt;ref&gt;/&lt;subpath&gt;</code>)
            or an archive URL ending in <code>.zip</code>, <code>.tar.gz</code>,
            <code>.tgz</code>, or <code>.tar.bz2</code>.
          </p>
          <div className="flex gap-2">
            <Input
              placeholder="https://github.com/owner/repo/tree/main/my-plugin"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !installMutation.isPending) {
                  handleInstall(false);
                }
              }}
              disabled={installMutation.isPending}
            />
            <Button
              onClick={() => handleInstall(false)}
              disabled={installMutation.isPending || !url.trim()}
            >
              {installMutation.isPending ? "Installing..." : "Install"}
            </Button>
          </div>
          {error && (
            <div className="flex items-start gap-2 text-xs text-destructive">
              <AlertCircleIcon className="h-3 w-3 mt-0.5 flex-shrink-0" />
              <div className="flex-1">
                <div>{error}</div>
                {error.toLowerCase().includes("already installed") && (
                  <button
                    type="button"
                    onClick={() => handleInstall(true)}
                    className="underline mt-1"
                    disabled={installMutation.isPending}
                  >
                    Reinstall (overwrite)
                  </button>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Plugin list */}
      <div className="space-y-3">
        {sorted.length === 0 && (
          <div className="text-sm text-muted-foreground text-center py-8">
            No plugins installed yet.
          </div>
        )}
        {sorted.map((p) => (
          <PluginCard
            key={p.name}
            plugin={p}
            onUninstall={() => uninstallMutation.mutate(p.name)}
            uninstalling={
              uninstallMutation.isPending &&
              uninstallMutation.variables === p.name
            }
          />
        ))}
      </div>
      </div>
    </div>
  );
}
