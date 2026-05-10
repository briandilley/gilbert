import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useWsApi } from "@/hooks/useWsApi";
import type { Feed } from "@/types/feeds";
import { Trash2Icon, AlertTriangleIcon } from "lucide-react";

interface Props {
  /** If null the drawer is in "create" mode. */
  feed: Feed | null;
  onClose: () => void;
  onSaved: () => void;
}

/** Feed create / edit / unsubscribe drawer.
 *
 * Create flow probes the URL via the backend (``feeds.create`` does
 * the probe + persist + start runtime in a single round-trip). Edit
 * flow exposes the user-facing knobs (name, category, importance,
 * poll interval, ingest_to_knowledge, briefing_eligible). Delete
 * cascades feed_items + knowledge entries on the server.
 */
export function FeedEditDrawer({ feed, onClose, onSaved }: Props) {
  const api = useWsApi();
  const isCreate = feed === null;

  const [url, setUrl] = useState(feed?.url ?? "");
  const [name, setName] = useState(feed?.name ?? "");
  const [category, setCategory] = useState(feed?.category ?? "");
  const [pollInterval, setPollInterval] = useState(
    feed?.poll_interval_sec ?? 1800,
  );
  const [importance, setImportance] = useState(feed?.importance_weight ?? 0.5);
  const [ingestToKnowledge, setIngestToKnowledge] = useState(
    feed?.ingest_to_knowledge ?? false,
  );
  const [briefingEligible, setBriefingEligible] = useState(
    feed?.briefing_eligible ?? true,
  );
  const [pollEnabled, setPollEnabled] = useState(feed?.poll_enabled ?? true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!feed) return;
    setUrl(feed.url);
    setName(feed.name);
    setCategory(feed.category);
    setPollInterval(feed.poll_interval_sec);
    setImportance(feed.importance_weight);
    setIngestToKnowledge(feed.ingest_to_knowledge);
    setBriefingEligible(feed.briefing_eligible);
    setPollEnabled(feed.poll_enabled);
  }, [feed]);

  const create = useMutation({
    mutationFn: () =>
      api.createFeed({
        url,
        name,
        category,
        poll_interval_sec: pollInterval,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to subscribe"),
  });

  const update = useMutation({
    mutationFn: () =>
      api.updateFeed(feed!.id, {
        name,
        category,
        poll_interval_sec: pollInterval,
        importance_weight: importance,
        ingest_to_knowledge: ingestToKnowledge,
        briefing_eligible: briefingEligible,
        poll_enabled: pollEnabled,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to update feed"),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteFeed(feed!.id),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to unsubscribe"),
  });

  const submit = () => {
    setError(null);
    if (isCreate && !url) {
      setError("Feed URL is required");
      return;
    }
    if (isCreate) {
      create.mutate();
    } else {
      update.mutate();
    }
  };

  return (
    <Sheet open onOpenChange={(open) => (!open ? onClose() : null)}>
      <SheetContent className="sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle>
            {isCreate ? "Subscribe to feed" : `Edit ${feed!.name}`}
          </SheetTitle>
        </SheetHeader>

        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <Label>Feed URL</Label>
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://example.com/feed.xml"
              disabled={!isCreate}
            />
          </div>

          <div className="space-y-2">
            <Label>Display name (optional)</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Auto-detected from feed if blank"
            />
          </div>

          <div className="space-y-2">
            <Label>Category (optional)</Label>
            <Input
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              placeholder="tech, news, …"
            />
          </div>

          <Separator />

          <div className="space-y-2">
            <Label>Poll interval (seconds)</Label>
            <Input
              type="number"
              min={60}
              value={pollInterval}
              onChange={(e) =>
                setPollInterval(parseInt(e.target.value, 10) || 1800)
              }
            />
            {feed?.suggested_poll_interval_sec ? (
              <p className="text-xs text-muted-foreground">
                Source suggests min {feed.suggested_poll_interval_sec}s; the
                effective cadence is {feed.effective_poll_interval_sec}s.
              </p>
            ) : null}
          </div>

          {!isCreate && (
            <>
              <div className="space-y-2">
                <Label>Importance weight (0.0–1.0)</Label>
                <Input
                  type="number"
                  min={0}
                  max={1}
                  step={0.1}
                  value={importance}
                  onChange={(e) =>
                    setImportance(parseFloat(e.target.value) || 0)
                  }
                />
                <p className="text-xs text-muted-foreground">
                  Multiplied into the AI score, so 0.0 silences a feed without
                  unsubscribing.
                </p>
              </div>

              <div className="space-y-2">
                <Label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={pollEnabled}
                    onChange={(e) => setPollEnabled(e.target.checked)}
                  />
                  Poll enabled
                </Label>
              </div>

              <div className="space-y-2">
                <Label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={briefingEligible}
                    onChange={(e) => setBriefingEligible(e.target.checked)}
                  />
                  Briefing eligible
                </Label>
                <p className="text-xs text-muted-foreground">
                  Feeds opt out of the briefing without losing scoring.
                </p>
              </div>

              <div className="space-y-2">
                <Label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={ingestToKnowledge}
                    onChange={(e) => setIngestToKnowledge(e.target.checked)}
                  />
                  Ingest into knowledge base
                </Label>
                <p className="text-xs text-muted-foreground">
                  Feeds article bodies into the vector index for cross-source
                  search. Subject to the per-user daily cap.
                </p>
              </div>
            </>
          )}

          {error && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertTriangleIcon className="h-4 w-4" />
              {error}
            </div>
          )}

          <div className="flex justify-between gap-2 pt-2">
            <div>
              {!isCreate && feed!.can_admin && (
                <Button
                  variant="destructive"
                  onClick={() => {
                    if (window.confirm(`Unsubscribe from ${feed!.name}?`)) {
                      remove.mutate();
                    }
                  }}
                >
                  <Trash2Icon className="h-4 w-4 mr-1" /> Unsubscribe
                </Button>
              )}
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button
                onClick={submit}
                disabled={create.isPending || update.isPending}
              >
                {isCreate ? "Subscribe" : "Save"}
              </Button>
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
