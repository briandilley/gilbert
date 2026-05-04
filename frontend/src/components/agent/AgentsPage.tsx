import { useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  PlusIcon,
  PlayIcon,
  Trash2Icon,
  PauseIcon,
  CheckCircle2Icon,
  ClockIcon,
  RadioIcon,
  ZapIcon,
  SparklesIcon,
  Loader2Icon,
} from "lucide-react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type {
  Goal,
  GoalCreatePayload,
  GoalStatus,
  TriggerConfig,
} from "@/types/agent";

const STATUS_BADGE: Record<GoalStatus, { label: string; cls: string; Icon: typeof CheckCircle2Icon }> = {
  enabled: { label: "Enabled", cls: "bg-green-500/10 text-green-700 dark:text-green-400", Icon: ZapIcon },
  disabled: { label: "Disabled", cls: "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400", Icon: PauseIcon },
  completed: { label: "Completed", cls: "bg-blue-500/10 text-blue-700 dark:text-blue-400", Icon: CheckCircle2Icon },
};

function describeTrigger(g: Goal): string {
  if (!g.trigger_type || g.trigger_type === "" || !g.trigger_config) return "Manual";
  const cfg = g.trigger_config as TriggerConfig;
  if (g.trigger_type === "time") {
    if (cfg.kind === "interval" && typeof cfg.seconds === "number") {
      const s = cfg.seconds;
      if (s % 3600 === 0) return `Every ${s / 3600}h`;
      if (s % 60 === 0) return `Every ${s / 60}m`;
      return `Every ${s}s`;
    }
    if (cfg.kind === "daily_at" && typeof cfg.hour === "number") {
      const m = String(cfg.minute ?? 0).padStart(2, "0");
      const h = String(cfg.hour).padStart(2, "0");
      return `Daily at ${h}:${m}`;
    }
    if (cfg.kind === "hourly_at" && typeof cfg.minute === "number") {
      return `Hourly at :${String(cfg.minute).padStart(2, "0")}`;
    }
    return "Time trigger";
  }
  if (g.trigger_type === "event") {
    if (Array.isArray(cfg.event_types) && cfg.event_types.length > 0) {
      return cfg.event_types.length === 1
        ? `On ${cfg.event_types[0]}`
        : `On ${cfg.event_types.length} events`;
    }
    return `On ${cfg.event_type ?? "event"}`;
  }
  return "Manual";
}

function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function AgentsPage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();
  const [createOpen, setCreateOpen] = useState(false);
  const [runningGoals, setRunningGoals] = useState<Set<string>>(new Set());

  const { data: goals, isLoading } = useQuery({
    queryKey: ["agent", "goals"],
    queryFn: api.listGoals,
    enabled: connected,
  });

  const { data: profiles } = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: api.listAiProfiles,
    enabled: connected,
  });

  const refreshOnRunComplete = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["agent"] });
  }, [queryClient]);

  useEventBus("agent.run.completed", refreshOnRunComplete);

  const handleRun = async (goalId: string) => {
    setRunningGoals((s) => new Set(s).add(goalId));
    try {
      await api.runGoalNow(goalId);
      queryClient.invalidateQueries({ queryKey: ["agent"] });
    } finally {
      setRunningGoals((s) => {
        const next = new Set(s);
        next.delete(goalId);
        return next;
      });
    }
  };

  const handleDelete = async (goalId: string) => {
    if (!confirm("Delete this goal? Its run history will also be deleted.")) return;
    await api.deleteGoal(goalId);
    queryClient.invalidateQueries({ queryKey: ["agent"] });
  };

  const handleToggle = async (g: Goal) => {
    if (g.status === "completed") return;
    const next: GoalStatus = g.status === "enabled" ? "disabled" : "enabled";
    await api.updateGoal(g.id, { status: next });
    queryClient.invalidateQueries({ queryKey: ["agent"] });
  };

  return (
    <div className="container mx-auto py-6 max-w-5xl">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-semibold">Agents</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Autonomous goals that run on schedules, in response to events,
            or on demand.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <PlusIcon className="size-4 mr-1" />
          New Goal
        </Button>
      </div>

      {isLoading ? (
        <div className="text-center text-muted-foreground py-8">Loading…</div>
      ) : !goals || goals.length === 0 ? (
        <div className="text-center text-muted-foreground py-12 border rounded-md">
          <p className="text-lg">No goals yet.</p>
          <p className="text-sm mt-1">Create one to get started.</p>
        </div>
      ) : (
        <div className="rounded-md border divide-y">
          {goals.map((g) => {
            const badge = STATUS_BADGE[g.status];
            return (
              <div
                key={g.id}
                className="flex items-start gap-3 px-4 py-3 hover:bg-accent/30"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <Link
                      to={`/agents/${g.id}`}
                      className="font-medium hover:underline"
                    >
                      {g.name}
                    </Link>
                    <span
                      className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs ${badge.cls}`}
                    >
                      <badge.Icon className="size-3" />
                      {badge.label}
                    </span>
                  </div>
                  <div className="text-xs text-muted-foreground mt-1 flex items-center gap-3 flex-wrap">
                    <span className="flex items-center gap-1">
                      {g.trigger_type === "time" ? (
                        <ClockIcon className="size-3" />
                      ) : g.trigger_type === "event" ? (
                        <RadioIcon className="size-3" />
                      ) : null}
                      {describeTrigger(g)}
                    </span>
                    <span>{g.run_count} run{g.run_count === 1 ? "" : "s"}</span>
                    <span>last: {timeAgo(g.last_run_at)}</span>
                    {g.last_run_status ? (
                      <span className="capitalize">{g.last_run_status}</span>
                    ) : null}
                  </div>
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleRun(g.id)}
                    disabled={
                      runningGoals.has(g.id) ||
                      g.status !== "enabled"
                    }
                    title="Run now"
                  >
                    <PlayIcon className="size-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => handleToggle(g)}
                    disabled={g.status === "completed"}
                    title={g.status === "enabled" ? "Disable" : "Enable"}
                  >
                    {g.status === "enabled" ? (
                      <PauseIcon className="size-4" />
                    ) : (
                      <ZapIcon className="size-4" />
                    )}
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => handleDelete(g.id)}
                    title="Delete"
                  >
                    <Trash2Icon className="size-4" />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <CreateGoalDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        profiles={profiles ?? []}
        onCreated={() => {
          queryClient.invalidateQueries({ queryKey: ["agent"] });
          setCreateOpen(false);
        }}
      />
    </div>
  );
}

interface CreateGoalDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  profiles: { name: string; description: string }[];
  onCreated: () => void;
}

function CreateGoalDialog({ open, onOpenChange, profiles, onCreated }: CreateGoalDialogProps) {
  const api = useWsApi();
  const [name, setName] = useState("");
  const [instruction, setInstruction] = useState("");
  const [profileId, setProfileId] = useState("");
  const [triggerKind, setTriggerKind] = useState<"manual" | "interval" | "daily_at" | "hourly_at" | "event">(
    "manual",
  );
  const [intervalSeconds, setIntervalSeconds] = useState(3600);
  const [dailyHour, setDailyHour] = useState(7);
  const [dailyMinute, setDailyMinute] = useState(0);
  const [hourlyMinute, setHourlyMinute] = useState(0);
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [eventTypeDraft, setEventTypeDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [authorOpen, setAuthorOpen] = useState(false);
  const [authorRequest, setAuthorRequest] = useState("");
  const [authoring, setAuthoring] = useState(false);
  const [authorError, setAuthorError] = useState<string | null>(null);

  const { data: observedEvents } = useQuery({
    queryKey: ["agent", "event-types"],
    queryFn: api.listObservedEventTypes,
    enabled: open,
  });

  const reset = () => {
    setName("");
    setInstruction("");
    setProfileId("");
    setTriggerKind("manual");
    setIntervalSeconds(3600);
    setDailyHour(7);
    setDailyMinute(0);
    setHourlyMinute(0);
    setEventTypes([]);
    setEventTypeDraft("");
    setError(null);
    setAuthorOpen(false);
    setAuthorRequest("");
    setAuthorError(null);
  };

  const handleAuthor = async () => {
    if (!authorRequest.trim()) {
      setAuthorError("Describe what you want changed.");
      return;
    }
    setAuthoring(true);
    setAuthorError(null);
    try {
      const result = await api.authorGoalInstruction(
        "", // empty: drafting a new goal
        instruction,
        authorRequest.trim(),
      );
      if (!result.ok || !result.new_text) {
        setAuthorError(result.error ?? "Author failed.");
        return;
      }
      setInstruction(result.new_text);
      setAuthorOpen(false);
      setAuthorRequest("");
    } catch (e) {
      setAuthorError(e instanceof Error ? e.message : String(e));
    } finally {
      setAuthoring(false);
    }
  };

  const handleSubmit = async () => {
    setError(null);
    if (!name.trim() || !instruction.trim() || !profileId) {
      setError("Name, instruction, and profile are required.");
      return;
    }
    setSubmitting(true);
    try {
      const payload: GoalCreatePayload = {
        name: name.trim(),
        instruction: instruction.trim(),
        profile_id: profileId,
      };
      if (triggerKind !== "manual") {
        if (triggerKind === "event") {
          if (eventTypes.length === 0) {
            setError("Pick at least one event type for event triggers.");
            setSubmitting(false);
            return;
          }
          payload.trigger_type = "event";
          payload.trigger_config = { event_types: eventTypes };
        } else {
          payload.trigger_type = "time";
          if (triggerKind === "interval") {
            payload.trigger_config = { kind: "interval", seconds: intervalSeconds };
          } else if (triggerKind === "daily_at") {
            payload.trigger_config = { kind: "daily_at", hour: dailyHour, minute: dailyMinute };
          } else {
            payload.trigger_config = { kind: "hourly_at", minute: hourlyMinute };
          }
        }
      }

      const result = await api.createGoal(payload);
      if (!result.ok) {
        setError(result.error ?? "Create failed.");
        return;
      }
      reset();
      onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Create Goal</DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <Label htmlFor="goal-name">Name</Label>
            <Input
              id="goal-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Watch overdue invoices"
            />
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <Label htmlFor="goal-instruction">Instruction</Label>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => {
                  setAuthorOpen((v) => !v);
                  setAuthorError(null);
                }}
              >
                <SparklesIcon className="size-3.5 mr-1" />
                Author with AI
              </Button>
            </div>
            <Textarea
              id="goal-instruction"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder="What should the agent do, and how does it know it's done?"
              rows={5}
            />
            {authorOpen ? (
              <div className="mt-2 rounded-md border bg-muted/30 p-3 space-y-2">
                <Label htmlFor="goal-author-request" className="text-xs">
                  How should the AI revise the instruction?
                </Label>
                <Textarea
                  id="goal-author-request"
                  value={authorRequest}
                  onChange={(e) => setAuthorRequest(e.target.value)}
                  placeholder="e.g. make this more specific about success criteria"
                  rows={2}
                />
                {authorError ? (
                  <div className="text-xs text-red-600">{authorError}</div>
                ) : null}
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setAuthorOpen(false);
                      setAuthorRequest("");
                      setAuthorError(null);
                    }}
                    disabled={authoring}
                  >
                    Cancel
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    onClick={handleAuthor}
                    disabled={authoring}
                  >
                    {authoring ? (
                      <>
                        <Loader2Icon className="size-3.5 mr-1 animate-spin" />
                        Authoring…
                      </>
                    ) : (
                      <>
                        <SparklesIcon className="size-3.5 mr-1" />
                        Apply
                      </>
                    )}
                  </Button>
                </div>
              </div>
            ) : null}
          </div>

          <div>
            <Label htmlFor="goal-profile">AI Profile</Label>
            <select
              id="goal-profile"
              value={profileId}
              onChange={(e) => setProfileId(e.target.value)}
              className="flex h-9 w-full rounded-md border bg-transparent px-3 py-1 text-sm shadow-sm"
            >
              <option value="">— Select a profile —</option>
              {profiles.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                  {p.description ? ` — ${p.description}` : ""}
                </option>
              ))}
            </select>
          </div>

          <div>
            <Label htmlFor="goal-trigger">Trigger</Label>
            <select
              id="goal-trigger"
              value={triggerKind}
              onChange={(e) =>
                setTriggerKind(e.target.value as typeof triggerKind)
              }
              className="flex h-9 w-full rounded-md border bg-transparent px-3 py-1 text-sm shadow-sm"
            >
              <option value="manual">Manual only</option>
              <option value="interval">Every N seconds</option>
              <option value="daily_at">Daily at HH:MM</option>
              <option value="hourly_at">Hourly at :MM</option>
              <option value="event">On event</option>
            </select>
          </div>

          {triggerKind === "interval" ? (
            <div>
              <Label htmlFor="goal-seconds">Interval (seconds)</Label>
              <Input
                id="goal-seconds"
                type="number"
                min={60}
                value={intervalSeconds}
                onChange={(e) => setIntervalSeconds(parseInt(e.target.value || "0", 10))}
              />
            </div>
          ) : null}

          {triggerKind === "daily_at" ? (
            <div className="flex gap-2">
              <div className="flex-1">
                <Label htmlFor="goal-hour">Hour</Label>
                <Input
                  id="goal-hour"
                  type="number"
                  min={0}
                  max={23}
                  value={dailyHour}
                  onChange={(e) => setDailyHour(parseInt(e.target.value || "0", 10))}
                />
              </div>
              <div className="flex-1">
                <Label htmlFor="goal-minute">Minute</Label>
                <Input
                  id="goal-minute"
                  type="number"
                  min={0}
                  max={59}
                  value={dailyMinute}
                  onChange={(e) => setDailyMinute(parseInt(e.target.value || "0", 10))}
                />
              </div>
            </div>
          ) : null}

          {triggerKind === "hourly_at" ? (
            <div>
              <Label htmlFor="goal-minute-h">Minute past the hour</Label>
              <Input
                id="goal-minute-h"
                type="number"
                min={0}
                max={59}
                value={hourlyMinute}
                onChange={(e) => setHourlyMinute(parseInt(e.target.value || "0", 10))}
              />
            </div>
          ) : null}

          {triggerKind === "event" ? (
            <div>
              <Label htmlFor="goal-event">Event types</Label>
              <p className="text-xs text-muted-foreground mb-2">
                Goal will fire on any of the selected events. Pick from
                the list of recently-observed events or type a custom
                event type.
              </p>

              {/* Selected events as chips */}
              {eventTypes.length > 0 ? (
                <div className="flex flex-wrap gap-1 mb-2">
                  {eventTypes.map((et) => (
                    <span
                      key={et}
                      className="inline-flex items-center gap-1 rounded-full border bg-muted px-2 py-0.5 text-xs"
                    >
                      <span className="font-mono">{et}</span>
                      <button
                        type="button"
                        onClick={() =>
                          setEventTypes((prev) => prev.filter((x) => x !== et))
                        }
                        className="text-muted-foreground hover:text-foreground"
                        aria-label={`Remove ${et}`}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              ) : null}

              {/* Custom-type input */}
              <div className="flex gap-2 mb-2">
                <Input
                  id="goal-event"
                  value={eventTypeDraft}
                  onChange={(e) => setEventTypeDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      const v = eventTypeDraft.trim();
                      if (v && !eventTypes.includes(v)) {
                        setEventTypes((prev) => [...prev, v]);
                      }
                      setEventTypeDraft("");
                    }
                  }}
                  placeholder="e.g. lead.created"
                  list="observed-event-types"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    const v = eventTypeDraft.trim();
                    if (v && !eventTypes.includes(v)) {
                      setEventTypes((prev) => [...prev, v]);
                    }
                    setEventTypeDraft("");
                  }}
                  disabled={!eventTypeDraft.trim()}
                >
                  Add
                </Button>
              </div>

              {/* Datalist for native autocomplete */}
              <datalist id="observed-event-types">
                {(observedEvents?.event_types ?? []).map((et) => (
                  <option key={et} value={et} />
                ))}
              </datalist>

              {/* Quick-add suggestions */}
              {observedEvents?.event_types && observedEvents.event_types.length > 0 ? (
                <div className="text-xs">
                  <div className="text-muted-foreground mb-1">
                    Recently observed:
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {observedEvents.event_types
                      .filter((et) => !eventTypes.includes(et))
                      .slice(0, 20)
                      .map((et) => (
                        <button
                          key={et}
                          type="button"
                          onClick={() => setEventTypes((prev) => [...prev, et])}
                          className="rounded-full border px-2 py-0.5 hover:bg-accent font-mono"
                        >
                          + {et}
                        </button>
                      ))}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}

          {error ? (
            <div className="text-sm text-red-600">{error}</div>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={submitting}>
            {submitting ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
