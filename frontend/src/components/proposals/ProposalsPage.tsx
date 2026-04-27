import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { MarkdownContent } from "@/components/ui/MarkdownContent";
import {
  RefreshCcwIcon,
  SparklesIcon,
  ChevronRightIcon,
  ChevronDownIcon,
  Trash2Icon,
  CopyIcon,
  CheckIcon,
} from "lucide-react";
import type { Proposal } from "@/types/proposals";

/** Status → Tailwind badge variant for the row chip. */
function statusVariant(
  status: string,
): "default" | "secondary" | "destructive" | "outline" {
  switch (status) {
    case "proposed":
      return "default";
    case "approved":
      return "secondary";
    case "implemented":
      return "secondary";
    case "rejected":
      return "destructive";
    case "archived":
      return "outline";
    default:
      return "outline";
  }
}

function formatTimestamp(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

const STATUS_OPTIONS = [
  "proposed",
  "approved",
  "rejected",
  "implemented",
  "archived",
] as const;

const KIND_OPTIONS = [
  "new_plugin",
  "modify_plugin",
  "remove_plugin",
  "new_service",
  "remove_service",
  "config_change",
] as const;

export function ProposalsPage() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();

  const [statusFilter, setStatusFilter] = useState<string>("");
  const [kindFilter, setKindFilter] = useState<string>("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [reflectionMessage, setReflectionMessage] = useState<string | null>(null);

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["proposals", statusFilter, kindFilter],
    queryFn: () =>
      api.listProposals({
        status: statusFilter || undefined,
        kind: kindFilter || undefined,
      }),
    enabled: connected,
    refetchInterval: 30_000,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["proposals"] });

  const updateStatusMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api.updateProposalStatus(id, status),
    onSuccess: invalidate,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteProposal(id),
    onSuccess: invalidate,
  });

  const triggerMutation = useMutation({
    mutationFn: () => api.triggerProposalReflection(),
    onSuccess: (result) => {
      // The reflection runs in the background — the AI round can take
      // tens of seconds, so the RPC returns immediately with a status.
      // New proposals show up via the periodic refetch.
      const message = (() => {
        switch (result.status) {
          case "started":
            return "Reflection started — new proposals will appear here as Gilbert finishes thinking.";
          case "already_running":
            return "A reflection cycle is already running. Hold tight.";
          case "disabled":
            return "Proposals service is disabled — enable it in Settings.";
          default:
            return `Reflection: ${result.status}`;
        }
      })();
      setReflectionMessage(message);
      invalidate();
      window.setTimeout(() => setReflectionMessage(null), 8000);
    },
    onError: (err: unknown) => {
      const message = err instanceof Error ? err.message : String(err);
      setReflectionMessage(`Reflection failed: ${message}`);
    },
  });

  if (isLoading) {
    return <LoadingSpinner text="Loading proposals..." className="p-4" />;
  }

  const proposals = data?.proposals ?? [];

  return (
    <div className="p-4 sm:p-6 max-w-6xl mx-auto">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold flex items-center gap-2">
            <SparklesIcon className="size-5 text-amber-500" />
            Proposals
          </h1>
          <p className="text-sm text-muted-foreground">
            Self-improvement ideas Gilbert generated based on observed
            activity. Review the spec, then approve, reject, or archive.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => triggerMutation.mutate()}
            disabled={triggerMutation.isPending}
            title="Run a reflection cycle now"
          >
            <SparklesIcon className="size-4 mr-1.5" />
            {triggerMutation.isPending ? "Reflecting…" : "Reflect now"}
          </Button>
          <Button variant="outline" size="sm" onClick={() => refetch()} title="Refresh">
            <RefreshCcwIcon className="size-4" />
          </Button>
        </div>
      </div>

      {reflectionMessage && (
        <div className="mb-4 rounded border bg-muted/40 px-3 py-2 text-sm">
          {reflectionMessage}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <FilterPills
          label="Status"
          options={STATUS_OPTIONS}
          value={statusFilter}
          onChange={setStatusFilter}
        />
        <div className="w-px h-5 bg-border mx-1" />
        <FilterPills
          label="Kind"
          options={KIND_OPTIONS}
          value={kindFilter}
          onChange={setKindFilter}
        />
      </div>

      {proposals.length === 0 ? (
        <Card>
          <CardContent className="p-8 text-center text-muted-foreground">
            No proposals match the current filters. Reflection runs on a
            schedule — Gilbert may not have proposed anything yet, or it
            decided there was nothing worth proposing.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {proposals.map((proposal) => (
            <ProposalRow
              key={proposal._id}
              proposal={proposal}
              expanded={expandedId === proposal._id}
              onToggle={() =>
                setExpandedId((prev) =>
                  prev === proposal._id ? null : proposal._id,
                )
              }
              onUpdateStatus={(status) =>
                updateStatusMutation.mutate({ id: proposal._id, status })
              }
              onDelete={() => deleteMutation.mutate(proposal._id)}
              busy={updateStatusMutation.isPending || deleteMutation.isPending}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface FilterPillsProps {
  label: string;
  options: readonly string[];
  value: string;
  onChange: (value: string) => void;
}

function FilterPills({ label, options, value, onChange }: FilterPillsProps) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs text-muted-foreground">{label}:</span>
      <Button
        size="sm"
        variant={value === "" ? "secondary" : "ghost"}
        className="h-7 px-2 text-xs"
        onClick={() => onChange("")}
      >
        all
      </Button>
      {options.map((option) => (
        <Button
          key={option}
          size="sm"
          variant={value === option ? "secondary" : "ghost"}
          className="h-7 px-2 text-xs"
          onClick={() => onChange(option)}
        >
          {option.replace(/_/g, " ")}
        </Button>
      ))}
    </div>
  );
}

interface ProposalRowProps {
  proposal: Proposal;
  expanded: boolean;
  onToggle: () => void;
  onUpdateStatus: (status: string) => void;
  onDelete: () => void;
  busy: boolean;
}

function ProposalRow({
  proposal,
  expanded,
  onToggle,
  onUpdateStatus,
  onDelete,
  busy,
}: ProposalRowProps) {
  return (
    <Card>
      <CardContent className="p-0">
        <button
          type="button"
          onClick={onToggle}
          className="w-full text-left p-3 flex items-start gap-2 hover:bg-muted/30"
        >
          {expanded ? (
            <ChevronDownIcon className="size-4 text-muted-foreground mt-1 shrink-0" />
          ) : (
            <ChevronRightIcon className="size-4 text-muted-foreground mt-1 shrink-0" />
          )}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium">{proposal.title}</span>
              <Badge variant={statusVariant(proposal.status)} className="text-xs">
                {proposal.status}
              </Badge>
              <Badge variant="outline" className="text-xs">
                {proposal.kind.replace(/_/g, " ")}
              </Badge>
              {proposal.target && (
                <Badge variant="outline" className="text-xs">
                  → {proposal.target}
                </Badge>
              )}
            </div>
            {proposal.summary && (
              <p className="text-sm text-muted-foreground mt-1 line-clamp-2">
                {proposal.summary}
              </p>
            )}
            <div className="text-xs text-muted-foreground mt-1">
              {formatTimestamp(proposal.created_at)}
            </div>
          </div>
        </button>
        {expanded && (
          <ProposalDetail
            proposal={proposal}
            onUpdateStatus={onUpdateStatus}
            onDelete={onDelete}
            busy={busy}
          />
        )}
      </CardContent>
    </Card>
  );
}

interface ProposalDetailProps {
  proposal: Proposal;
  onUpdateStatus: (status: string) => void;
  onDelete: () => void;
  busy: boolean;
}

function ProposalDetail({
  proposal,
  onUpdateStatus,
  onDelete,
  busy,
}: ProposalDetailProps) {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [copied, setCopied] = useState(false);

  const noteMutation = useMutation({
    mutationFn: (text: string) => api.addProposalNote(proposal._id, text),
    onSuccess: () => {
      setNote("");
      queryClient.invalidateQueries({ queryKey: ["proposals"] });
    },
  });

  const [copyError, setCopyError] = useState<string | null>(null);
  const copyImplementationPrompt = async () => {
    setCopyError(null);
    const text = proposal.implementation_prompt;
    // Modern API — only available on HTTPS or localhost.
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
        return;
      } catch (err) {
        // Fall through to the legacy path below.
        console.warn("clipboard.writeText failed, falling back", err);
      }
    }
    // Fallback for non-secure contexts (LAN install over plain HTTP):
    // a hidden textarea + document.execCommand("copy"). Deprecated but
    // still works in every browser we care about.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    document.body.removeChild(ta);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } else {
      setCopyError(
        "Couldn't copy automatically — select the text below and press Ctrl/Cmd+C.",
      );
    }
  };

  return (
    <div className="border-t bg-muted/20 p-4 space-y-4">
      {proposal.motivation && (
        <Section title="Motivation">
          <p className="text-sm whitespace-pre-wrap">{proposal.motivation}</p>
        </Section>
      )}

      {proposal.evidence?.length > 0 && (
        <Section title="Evidence">
          <ul className="text-sm space-y-1">
            {proposal.evidence.map((ev, idx) => (
              <li key={idx} className="font-mono text-xs">
                <span className="text-muted-foreground">
                  {ev.event_type} ({ev.count}× · {formatTimestamp(ev.occurred_at)})
                </span>
                {ev.summary && <>: {ev.summary}</>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Spec">
        <pre className="text-xs rounded bg-background border p-3 overflow-x-auto max-h-96">
          {JSON.stringify(proposal.spec, null, 2)}
        </pre>
      </Section>

      {proposal.acceptance_criteria?.length > 0 && (
        <Section title="Acceptance criteria">
          <ul className="text-sm list-disc pl-5 space-y-0.5">
            {proposal.acceptance_criteria.map((c, idx) => (
              <li key={idx}>{c}</li>
            ))}
          </ul>
        </Section>
      )}

      {proposal.risks?.length > 0 && (
        <Section title="Risks">
          <ul className="text-sm space-y-2">
            {proposal.risks.map((r, idx) => (
              <li key={idx} className="border-l-2 border-amber-500/50 pl-2">
                <Badge variant="outline" className="text-xs mr-2">
                  {r.category}
                </Badge>
                <span>{r.description}</span>
                {r.mitigation && (
                  <div className="text-xs text-muted-foreground mt-0.5">
                    Mitigation: {r.mitigation}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {proposal.open_questions?.length > 0 && (
        <Section title="Open questions">
          <ul className="text-sm list-disc pl-5 space-y-0.5">
            {proposal.open_questions.map((q, idx) => (
              <li key={idx}>{q}</li>
            ))}
          </ul>
        </Section>
      )}

      <Section
        title="Implementation prompt"
        action={
          <Button
            size="sm"
            variant="outline"
            onClick={copyImplementationPrompt}
            className="h-7 px-2 text-xs"
          >
            {copied ? (
              <>
                <CheckIcon className="size-3 mr-1" /> Copied
              </>
            ) : (
              <>
                <CopyIcon className="size-3 mr-1" /> Copy
              </>
            )}
          </Button>
        }
      >
        <p className="text-xs text-muted-foreground mb-2">
          Self-contained prompt — paste into a fresh Claude Code session
          to implement this proposal.
        </p>
        {copyError && (
          <div className="text-xs text-destructive mb-2">{copyError}</div>
        )}
        <div className="rounded border bg-background p-3 max-h-96 overflow-auto">
          <MarkdownContent content={proposal.implementation_prompt} />
        </div>
        {/* Always render the raw text in a hidden-but-selectable
            textarea so users on insecure-context installs can manually
            select-all and copy when the automatic copy is blocked. */}
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            Show raw text (for manual copy)
          </summary>
          <textarea
            readOnly
            value={proposal.implementation_prompt}
            className="mt-2 w-full font-mono text-xs rounded border bg-background p-2 h-48"
            onFocus={(e) => e.currentTarget.select()}
          />
        </details>
      </Section>

      {proposal.admin_notes?.length > 0 && (
        <Section title="Notes">
          <ul className="text-sm space-y-2">
            {proposal.admin_notes.map((n, idx) => (
              <li key={idx} className="border-l-2 border-muted-foreground/30 pl-2">
                <div className="text-xs text-muted-foreground">
                  {n.author_id || "(unknown)"} · {formatTimestamp(n.added_at)}
                </div>
                <div className="whitespace-pre-wrap">{n.note}</div>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <div className="flex flex-wrap gap-2 items-end pt-2 border-t">
        <div className="flex-1 min-w-[200px]">
          <label className="text-xs text-muted-foreground mb-1 block">
            Add a note
          </label>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            className="w-full text-sm rounded border bg-background p-2"
            placeholder="Decision rationale, follow-ups, …"
          />
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={!note.trim() || noteMutation.isPending}
          onClick={() => noteMutation.mutate(note.trim())}
        >
          Add note
        </Button>
      </div>

      <div className="flex flex-wrap gap-2 items-center justify-between pt-2 border-t">
        <div className="flex flex-wrap gap-1.5">
          {STATUS_OPTIONS.filter((s) => s !== proposal.status).map((s) => (
            <Button
              key={s}
              size="sm"
              variant={s === "rejected" ? "destructive" : "outline"}
              disabled={busy}
              onClick={() => onUpdateStatus(s)}
            >
              Set {s}
            </Button>
          ))}
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="text-destructive"
          disabled={busy}
          onClick={() => {
            if (window.confirm(`Delete proposal "${proposal.title}"?`)) {
              onDelete();
            }
          }}
        >
          <Trash2Icon className="size-3 mr-1" />
          Delete
        </Button>
      </div>
    </div>
  );
}

interface SectionProps {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}

function Section({ title, action, children }: SectionProps) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </h3>
        {action}
      </div>
      {children}
    </div>
  );
}
