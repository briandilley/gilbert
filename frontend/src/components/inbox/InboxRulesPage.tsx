import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { PageHeader } from "@/components/layout/PageHeader";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { InboxRule, InboxRuleRun } from "@/types/inboxRules";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const STATUS_STYLE: Record<string, string> = {
  done: "bg-emerald-500/15 text-emerald-600",
  dry_run: "bg-sky-500/15 text-sky-600",
  held: "bg-amber-500/15 text-amber-600",
  retry: "bg-amber-500/15 text-amber-600",
  failed: "bg-rose-500/15 text-rose-600",
};

function emptyRule(): InboxRule {
  return {
    id: "",
    name: "",
    enabled: true,
    dry_run: true,
    mailbox_ids: [],
    sender_contains: [],
    subject_contains: [],
    match_condition: "",
    match_profile: "light",
    action_instructions: "",
    action_tools: [],
    action_profile: "standard",
    announce: true,
    speakers: [],
    active_days: [0, 1, 2, 3, 4],
    start_hour: 8,
    end_hour: 18,
    timezone: "America/Los_Angeles",
    max_age_hours: 4,
    dedupe_window_hours: 72,
    created_at: "",
    updated_at: "",
  };
}

/** Comma-separated text ⇄ list, for the free-filter fields. */
function toList(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

function formatTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function CheckList({
  items,
  selected,
  onChange,
  empty,
}: {
  items: Array<{ value: string; label: string; hint?: string }>;
  selected: string[];
  onChange: (next: string[]) => void;
  empty: string;
}) {
  if (items.length === 0) {
    return <p className="text-xs text-muted-foreground">{empty}</p>;
  }
  return (
    <div className="max-h-48 overflow-y-auto rounded-md border p-2 space-y-1">
      {items.map((it) => (
        <label key={it.value} className="flex items-start gap-2 text-xs cursor-pointer">
          <input
            type="checkbox"
            className="accent-primary mt-0.5"
            checked={selected.includes(it.value)}
            onChange={() => onChange(toggle(selected, it.value))}
          />
          <span>
            <span className="font-mono">{it.label}</span>
            {it.hint && <span className="text-muted-foreground"> — {it.hint}</span>}
          </span>
        </label>
      ))}
    </div>
  );
}

function ProfileSelect({
  value,
  profiles,
  onChange,
}: {
  value: string;
  profiles: string[];
  onChange: (v: string) => void;
}) {
  const options = profiles.includes(value) || !value ? profiles : [value, ...profiles];
  return (
    <Select value={value} onValueChange={(v) => v && onChange(v)}>
      <SelectTrigger>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((p) => (
          <SelectItem key={p} value={p}>
            {p}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function RuleEditor({
  rule,
  onClose,
}: {
  rule: InboxRule;
  onClose: () => void;
}) {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();
  const [form, setForm] = useState<InboxRule>(rule);
  const [senders, setSenders] = useState(rule.sender_contains.join(", "));
  const [subjects, setSubjects] = useState(rule.subject_contains.join(", "));

  const { data: options } = useQuery({
    queryKey: ["inbox-rule-options"],
    queryFn: api.getInboxRuleOptions,
    enabled: connected,
  });

  const save = useMutation({
    mutationFn: () =>
      api.saveInboxRule({
        ...form,
        sender_contains: toList(senders),
        subject_contains: toList(subjects),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inbox-rules"] });
      onClose();
    },
  });

  const set = <K extends keyof InboxRule>(key: K, value: InboxRule[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const hasFilter = toList(senders).length + toList(subjects).length > 0;

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{rule.id ? `Edit "${rule.name}"` : "New inbox rule"}</DialogTitle>
        </DialogHeader>

        <div className="space-y-5">
          <div className="space-y-1.5">
            <Label className="text-xs">Name</Label>
            <Input value={form.name} onChange={(e) => set("name", e.target.value)} />
          </div>

          <div className="flex flex-wrap gap-4 text-xs">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                className="accent-primary"
                checked={form.enabled}
                onChange={(e) => set("enabled", e.target.checked)}
              />
              Enabled
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                className="accent-primary"
                checked={form.dry_run}
                onChange={(e) => set("dry_run", e.target.checked)}
              />
              Dry run (log what it would say, do not speak)
            </label>
          </div>

          <section className="space-y-3">
            <h3 className="text-sm font-medium">1. Free filter</h3>
            <p className="text-[11px] text-muted-foreground">
              Only email that passes this filter reaches the AI. Comma-separated,
              case-insensitive. Set at least one of the two.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label className="text-xs">Sender contains any of</Label>
                <Input
                  value={senders}
                  onChange={(e) => setSenders(e.target.value)}
                  placeholder="amazon, ups, fedex"
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Subject contains any of</Label>
                <Input
                  value={subjects}
                  onChange={(e) => setSubjects(e.target.value)}
                  placeholder="delivered, delivery"
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Mailboxes (none checked = all)</Label>
              <CheckList
                items={(options?.mailboxes ?? []).map((m) => ({ value: m.id, label: m.name }))}
                selected={form.mailbox_ids}
                onChange={(v) => set("mailbox_ids", v)}
                empty="No mailboxes."
              />
            </div>
          </section>

          <section className="space-y-3">
            <h3 className="text-sm font-medium">2. Match</h3>
            <div className="space-y-1.5">
              <Label className="text-xs">Condition (plain language)</Label>
              <Textarea
                rows={3}
                value={form.match_condition}
                onChange={(e) => set("match_condition", e.target.value)}
                placeholder="The email says a package was delivered, or that a delivery failed or was delayed."
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">AI profile for the match call</Label>
              <ProfileSelect
                value={form.match_profile}
                profiles={options?.profiles ?? []}
                onChange={(v) => set("match_profile", v)}
              />
            </div>
          </section>

          <section className="space-y-3">
            <h3 className="text-sm font-medium">3. Action</h3>
            <div className="space-y-1.5">
              <Label className="text-xs">Instructions</Label>
              <Textarea
                rows={6}
                value={form.action_instructions}
                onChange={(e) => set("action_instructions", e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Allowed tools</Label>
              <CheckList
                items={(options?.tools ?? []).map((t) => ({
                  value: t.name,
                  label: t.name,
                  hint: t.description.slice(0, 90),
                }))}
                selected={form.action_tools}
                onChange={(v) => set("action_tools", v)}
                empty="No tools available."
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">AI profile for the action turn</Label>
              <ProfileSelect
                value={form.action_profile}
                profiles={options?.profiles ?? []}
                onChange={(v) => set("action_profile", v)}
              />
            </div>
          </section>

          <section className="space-y-3">
            <h3 className="text-sm font-medium">Announcement</h3>
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input
                type="checkbox"
                className="accent-primary"
                checked={form.announce}
                onChange={(e) => set("announce", e.target.checked)}
              />
              Speak the action's reply on the speakers
            </label>
            <div className="space-y-1.5">
              <Label className="text-xs">Speakers (none checked = default speakers)</Label>
              <CheckList
                items={(options?.speakers ?? []).map((s) => ({ value: s, label: s }))}
                selected={form.speakers}
                onChange={(v) => set("speakers", v)}
                empty="No speakers found."
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Days</Label>
              <div className="flex flex-wrap gap-1">
                {DAYS.map((d, i) => (
                  <Button
                    key={d}
                    type="button"
                    size="xs"
                    variant={form.active_days.includes(i) ? "default" : "outline"}
                    onClick={() => set("active_days", toggle(form.active_days, i).sort())}
                  >
                    {d}
                  </Button>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <div className="space-y-1.5">
                <Label className="text-xs">Start hour</Label>
                <Input
                  type="number"
                  min={0}
                  max={24}
                  value={form.start_hour}
                  onChange={(e) => set("start_hour", parseInt(e.target.value, 10) || 0)}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">End hour</Label>
                <Input
                  type="number"
                  min={0}
                  max={24}
                  value={form.end_hour}
                  onChange={(e) => set("end_hour", parseInt(e.target.value, 10) || 0)}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Drop after (hours)</Label>
                <Input
                  type="number"
                  min={0}
                  step="0.5"
                  value={form.max_age_hours}
                  onChange={(e) => set("max_age_hours", parseFloat(e.target.value) || 0)}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Dedupe window (hours)</Label>
                <Input
                  type="number"
                  min={0}
                  value={form.dedupe_window_hours}
                  onChange={(e) =>
                    set("dedupe_window_hours", parseFloat(e.target.value) || 0)
                  }
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Timezone</Label>
              <Input value={form.timezone} onChange={(e) => set("timezone", e.target.value)} />
            </div>
          </section>
        </div>

        {save.isError && (
          <p className="text-sm text-rose-500">
            Could not save: {(save.error as Error)?.message || "unknown error"}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => save.mutate()}
            disabled={!form.name.trim() || !form.match_condition.trim() || !hasFilter}
          >
            {rule.id ? "Save" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RunsList({ rules }: { rules: InboxRule[] }) {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [ruleId, setRuleId] = useState("__all__");
  const { data, isLoading } = useQuery({
    queryKey: ["inbox-rule-runs", ruleId],
    queryFn: () => api.listInboxRuleRuns(ruleId === "__all__" ? undefined : ruleId, 200),
    enabled: connected,
    refetchInterval: 30_000,
  });

  return (
    <div className="space-y-3">
      <div className="max-w-xs">
        <Select value={ruleId} onValueChange={(v) => v && setRuleId(v)}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">All rules</SelectItem>
            {rules.map((r) => (
              <SelectItem key={r.id} value={r.id}>
                {r.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {isLoading && <LoadingSpinner text="Loading activity..." className="p-4" />}
      {data?.runs.length === 0 && (
        <p className="text-sm text-muted-foreground">No email has reached a rule yet.</p>
      )}
      <div className="space-y-2">
        {data?.runs.map((run: InboxRuleRun) => (
          <Card key={run._id}>
            <CardContent className="py-3 space-y-1 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="secondary" className={STATUS_STYLE[run.status] ?? ""}>
                  {run.status.replace("_", " ")}
                </Badge>
                <span className="font-medium break-all">{run.subject || "(no subject)"}</span>
              </div>
              <div className="text-xs text-muted-foreground">
                {run.rule_name} · {run.sender} · {formatTime(run.received_at)}
                {run.attempts > 1 && ` · ${run.attempts} attempts`}
              </div>
              {run.reason && <div className="text-xs">Match: {run.reason}</div>}
              {run.output && <div className="text-xs italic">“{run.output}”</div>}
              {run.error && <div className="text-xs text-rose-500 break-all">{run.error}</div>}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

export function InboxRulesPage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();
  const [editing, setEditing] = useState<InboxRule | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["inbox-rules"],
    queryFn: api.listInboxRules,
    enabled: connected,
  });
  const remove = useMutation({
    mutationFn: api.deleteInboxRule,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["inbox-rules"] }),
  });
  const rules = data?.rules ?? [];

  return (
    <div>
      <PageHeader
        eyebrow="INBOX"
        title="Inbox rules"
        description="React to new email: a free filter first, then a small AI match, then a tool-limited AI action whose reply is announced."
        actions={
          <div className="flex items-center gap-2">
            <PluginPanelSlot slot="inbox-rules.toolbar" />
            <Button size="sm" onClick={() => setEditing(emptyRule())}>
              <PlusIcon />
              New rule
            </Button>
          </div>
        }
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        <Tabs defaultValue="rules">
          <TabsList>
            <TabsTrigger value="rules">Rules</TabsTrigger>
            <TabsTrigger value="activity">Activity</TabsTrigger>
          </TabsList>
          <TabsContent value="rules" className="pt-4">
            {isLoading && <LoadingSpinner text="Loading rules..." className="p-4" />}
            {!isLoading && rules.length === 0 && (
              <p className="text-sm text-muted-foreground">No inbox rules yet.</p>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {rules.map((r) => (
                <Card key={r.id}>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm flex items-center gap-2">
                      <span className="flex-1">{r.name}</span>
                      {!r.enabled && (
                        <Badge variant="outline" className="text-xs">
                          disabled
                        </Badge>
                      )}
                      {r.dry_run && (
                        <Badge variant="outline" className="text-xs">
                          dry run
                        </Badge>
                      )}
                      <Button variant="ghost" size="icon-xs" onClick={() => setEditing(r)}>
                        <PencilIcon className="size-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        className="text-destructive"
                        onClick={() => {
                          if (window.confirm(`Delete the rule "${r.name}"?`)) remove.mutate(r.id);
                        }}
                      >
                        <Trash2Icon className="size-3" />
                      </Button>
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-1 text-xs text-muted-foreground">
                    <p>{r.match_condition}</p>
                    <p>
                      Filter: {[...r.sender_contains, ...r.subject_contains].join(", ")}
                    </p>
                    <p>
                      {r.announce ? "Announces" : "Does not announce"} ·{" "}
                      {r.active_days.map((d) => DAYS[d]).join(" ")} {r.start_hour}–{r.end_hour}
                    </p>
                  </CardContent>
                </Card>
              ))}
            </div>
          </TabsContent>
          <TabsContent value="activity" className="pt-4">
            <RunsList rules={rules} />
          </TabsContent>
        </Tabs>
      </div>
      {editing && <RuleEditor rule={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
