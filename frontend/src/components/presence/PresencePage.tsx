import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi, type PresenceThing } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CheckIcon, PencilIcon, RefreshCwIcon, XIcon } from "lucide-react";

/**
 * Admin page for mapping raw presence observations to Gilbert users.
 *
 * Left pane: things the backends are reporting that no user is mapped
 * to yet (sorted by most recently seen first). Right pane: things
 * already mapped, grouped by the user they belong to. Mapping happens
 * via a per-row select; cosmetic relabel is inline-editable on the
 * row itself.
 *
 * RBAC is enforced server-side on every WS RPC — non-admins who
 * navigate here directly will see empty lists and 403 errors from
 * the API. The nav card is also filtered out for them upstream.
 */
export function PresencePage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();

  const { data: things, isLoading } = useQuery({
    queryKey: ["presence-things"],
    queryFn: () => api.listPresenceThings("all"),
    enabled: connected,
    refetchInterval: 15_000,
  });

  const { data: users } = useQuery({
    queryKey: ["chat-users"],
    queryFn: api.listChatUsers,
    enabled: connected,
  });

  const mapMut = useMutation({
    mutationFn: ({
      backend,
      thingId,
      userId,
    }: {
      backend: string;
      thingId: string;
      userId: string;
    }) => api.mapPresenceThing(backend, thingId, userId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["presence-things"] }),
  });

  const unmapMut = useMutation({
    mutationFn: ({ backend, thingId }: { backend: string; thingId: string }) =>
      api.unmapPresenceThing(backend, thingId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["presence-things"] }),
  });

  const relabelMut = useMutation({
    mutationFn: ({
      backend,
      thingId,
      label,
    }: {
      backend: string;
      thingId: string;
      label: string;
    }) => api.relabelPresenceThing(backend, thingId, label),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["presence-things"] }),
  });

  const pollMut = useMutation({
    mutationFn: api.forcePresencePoll,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["presence-things"] }),
  });

  if (isLoading || !things) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <LoadingSpinner />
      </div>
    );
  }

  const unmapped = things.filter((t) => !t.mapped_user_id);
  const mapped = things.filter((t) => !!t.mapped_user_id);

  // Group mapped rows by user_id so each user owns a section.
  const mappedByUser = new Map<string, PresenceThing[]>();
  for (const t of mapped) {
    const arr = mappedByUser.get(t.mapped_user_id) ?? [];
    arr.push(t);
    mappedByUser.set(t.mapped_user_id, arr);
  }
  const userNameById = new Map<string, string>(
    (users ?? []).map((u) => [u.user_id, u.display_name || u.user_id]),
  );

  return (
    <div className="p-4 sm:p-6 space-y-4 sm:space-y-6 max-w-6xl mx-auto">
      <div className="flex flex-wrap items-start gap-4 justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold">Presence mapping</h1>
          <p className="text-sm text-muted-foreground">
            Map the raw things the presence backends see to Gilbert users.
            Backends consult these mappings on the next poll, so changes take
            effect within seconds. Hit "Poll now" if you don't want to wait.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => pollMut.mutate()}
          disabled={pollMut.isPending}
        >
          <RefreshCwIcon
            className={pollMut.isPending ? "size-4 animate-spin" : "size-4"}
          />
          Poll now
        </Button>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 sm:gap-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Unmapped things
              <Badge variant="secondary">{unmapped.length}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {unmapped.length === 0 && (
              <div className="text-sm text-muted-foreground">
                Nothing here — every detected thing has a user mapped to it.
              </div>
            )}
            {unmapped.map((t) => (
              <ThingRow
                key={`${t.backend}:${t.thing_id}`}
                thing={t}
                users={users ?? []}
                onMap={(userId) =>
                  mapMut.mutate({
                    backend: t.backend,
                    thingId: t.thing_id,
                    userId,
                  })
                }
                onRelabel={(label) =>
                  relabelMut.mutate({
                    backend: t.backend,
                    thingId: t.thing_id,
                    label,
                  })
                }
              />
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Mapped to users
              <Badge variant="secondary">{mapped.length}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {mapped.length === 0 && (
              <div className="text-sm text-muted-foreground">
                No mappings yet. Map something from the left to get started.
              </div>
            )}
            {Array.from(mappedByUser.entries())
              .sort(([a], [b]) => {
                const an = userNameById.get(a) ?? a;
                const bn = userNameById.get(b) ?? b;
                return an.localeCompare(bn);
              })
              .map(([userId, rows]) => (
                <div key={userId} className="space-y-2">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">
                      {userNameById.get(userId) ?? userId}
                    </span>
                    <Badge variant="outline">{rows.length}</Badge>
                  </div>
                  {rows.map((t) => (
                    <ThingRow
                      key={`${t.backend}:${t.thing_id}`}
                      thing={t}
                      users={users ?? []}
                      onMap={(uid) =>
                        mapMut.mutate({
                          backend: t.backend,
                          thingId: t.thing_id,
                          userId: uid,
                        })
                      }
                      onUnmap={() =>
                        unmapMut.mutate({
                          backend: t.backend,
                          thingId: t.thing_id,
                        })
                      }
                      onRelabel={(label) =>
                        relabelMut.mutate({
                          backend: t.backend,
                          thingId: t.thing_id,
                          label,
                        })
                      }
                    />
                  ))}
                </div>
              ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

interface ThingRowProps {
  thing: PresenceThing;
  users: { user_id: string; display_name: string }[];
  onMap: (userId: string) => void;
  onRelabel: (label: string) => void;
  onUnmap?: () => void;
}

function ThingRow({ thing, users, onMap, onRelabel, onUnmap }: ThingRowProps) {
  const [editingLabel, setEditingLabel] = useState(false);
  const [labelDraft, setLabelDraft] = useState(thing.label);

  function commitLabel() {
    if (labelDraft.trim() && labelDraft.trim() !== thing.label) {
      onRelabel(labelDraft.trim());
    }
    setEditingLabel(false);
  }

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border p-3 text-sm">
      <Badge variant="outline" className="shrink-0 uppercase">
        {thing.kind || "thing"}
      </Badge>
      <div className="flex-1 min-w-[12rem]">
        {editingLabel ? (
          <div className="flex items-center gap-1">
            <Input
              value={labelDraft}
              onChange={(e) => setLabelDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitLabel();
                if (e.key === "Escape") {
                  setLabelDraft(thing.label);
                  setEditingLabel(false);
                }
              }}
              className="h-7"
            />
            <Button size="icon" variant="ghost" onClick={commitLabel}>
              <CheckIcon className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => {
                setLabelDraft(thing.label);
                setEditingLabel(false);
              }}
            >
              <XIcon className="h-4 w-4" />
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{thing.label || thing.thing_id}</span>
            <Button
              size="icon"
              variant="ghost"
              className="h-6 w-6"
              onClick={() => setEditingLabel(true)}
            >
              <PencilIcon className="h-3 w-3" />
            </Button>
          </div>
        )}
        <div className="text-xs text-muted-foreground truncate">
          <code>{thing.backend}</code> · last seen {formatRelative(thing.last_seen)}
        </div>
      </div>
      <Select
        value={thing.mapped_user_id || "__unmapped__"}
        onValueChange={(v: string | null) => {
          if (!v || v === "__unmapped__") {
            onUnmap?.();
          } else {
            onMap(v);
          }
        }}
      >
        <SelectTrigger className="w-44">
          <SelectValue placeholder="Map to user…">
            {(v: string | null) => {
              if (!v || v === "__unmapped__") return "Map to user…";
              const u = users.find((x) => x.user_id === v);
              return u ? u.display_name || u.user_id : v;
            }}
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="__unmapped__">— unmapped —</SelectItem>
          {users.map((u) => (
            <SelectItem key={u.user_id} value={u.user_id}>
              {u.display_name || u.user_id}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function formatRelative(iso: string): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
