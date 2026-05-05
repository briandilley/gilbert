import { useMemo, useState } from "react";
import {
  useAgentCommitments,
  useCompleteCommitment,
  useCreateCommitment,
} from "@/api/agents";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { timeAgo } from "@/lib/timeAgo";
import type { Commitment } from "@/types/agent";

interface Props {
  agentId: string;
}

type DurationUnit = "h" | "d";

export function CommitmentsList({ agentId }: Props) {
  const commitmentsQuery = useAgentCommitments(agentId, true);
  const createCommitment = useCreateCommitment();
  const completeCommitment = useCompleteCommitment();

  const [content, setContent] = useState("");
  const [dueAmount, setDueAmount] = useState<string>("1");
  const [dueUnit, setDueUnit] = useState<DurationUnit>("h");
  const [showCompleted, setShowCompleted] = useState(false);
  const [completingId, setCompletingId] = useState<string | null>(null);
  const [completionNote, setCompletionNote] = useState("");

  const { active, completed } = useMemo(() => {
    const a: Commitment[] = [];
    const c: Commitment[] = [];
    for (const item of commitmentsQuery.data ?? []) {
      if (item.completed_at == null) a.push(item);
      else c.push(item);
    }
    // Active: by due date ascending. Completed: by completed_at desc.
    a.sort((x, y) => x.due_at.localeCompare(y.due_at));
    c.sort((x, y) =>
      (y.completed_at ?? "").localeCompare(x.completed_at ?? ""),
    );
    return { active: a, completed: c };
  }, [commitmentsQuery.data]);

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault();
    const text = content.trim();
    if (!text) return;
    const amount = Math.max(0, Number(dueAmount) || 0);
    const seconds = dueUnit === "d" ? amount * 86400 : amount * 3600;
    createCommitment.mutate(
      {
        agentId,
        content: text,
        ...(seconds > 0 ? { dueInSeconds: seconds } : {}),
      },
      {
        onSuccess: () => {
          setContent("");
          setDueAmount("1");
          setDueUnit("h");
        },
      },
    );
  };

  const startComplete = (commitment: Commitment) => {
    setCompletingId(commitment._id);
    setCompletionNote("");
  };

  const cancelComplete = () => {
    setCompletingId(null);
    setCompletionNote("");
  };

  const confirmComplete = (commitment: Commitment) => {
    const note = completionNote.trim();
    completeCommitment.mutate(
      {
        commitmentId: commitment._id,
        ...(note ? { note } : {}),
      },
      {
        onSuccess: () => {
          setCompletingId(null);
          setCompletionNote("");
        },
      },
    );
  };

  return (
    <div className="space-y-4">
      <form
        onSubmit={handleAdd}
        className="rounded-md border p-3 space-y-2"
        aria-label="Quick add commitment"
      >
        <div className="text-sm font-medium">Quick add</div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <div className="flex-1">
            <Label htmlFor="commitment-content" className="text-xs">
              What
            </Label>
            <Input
              id="commitment-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Follow up with the vendor"
            />
          </div>
          <div className="w-24">
            <Label htmlFor="commitment-due-amount" className="text-xs">
              Due in
            </Label>
            <Input
              id="commitment-due-amount"
              type="number"
              min={0}
              value={dueAmount}
              onChange={(e) => setDueAmount(e.target.value)}
            />
          </div>
          <div className="w-20">
            <Label htmlFor="commitment-due-unit" className="text-xs">
              Unit
            </Label>
            <select
              id="commitment-due-unit"
              value={dueUnit}
              onChange={(e) => setDueUnit(e.target.value as DurationUnit)}
              className="h-8 w-full rounded-md border border-input bg-transparent px-2 text-sm"
            >
              <option value="h">hours</option>
              <option value="d">days</option>
            </select>
          </div>
          <Button
            type="submit"
            disabled={createCommitment.isPending || !content.trim()}
          >
            Add
          </Button>
        </div>
        {createCommitment.isError && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {createCommitment.error instanceof Error
              ? createCommitment.error.message
              : "Failed to create commitment."}
          </div>
        )}
      </form>

      {commitmentsQuery.isPending && <LoadingSpinner text="Loading…" />}

      {commitmentsQuery.isError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Failed to load commitments.
        </div>
      )}

      <section className="space-y-2">
        <h3 className="text-sm font-medium">
          Active{" "}
          <span className="text-muted-foreground">({active.length})</span>
        </h3>
        {completeCommitment.isError && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {completeCommitment.error instanceof Error
              ? completeCommitment.error.message
              : "Failed to complete commitment."}
          </div>
        )}
        {active.length === 0 ? (
          <div className="rounded-md border border-dashed px-4 py-4 text-center text-sm text-muted-foreground">
            No active commitments.
          </div>
        ) : (
          <ul className="rounded-md border divide-y">
            {active.map((c) => {
              const isCompleting = completingId === c._id;
              return (
                <li key={c._id} className="px-3 py-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="text-sm">{c.content}</div>
                      <div className="text-xs text-muted-foreground">
                        Due {timeAgo(c.due_at)}
                      </div>
                    </div>
                    {!isCompleting && (
                      <Button
                        variant="outline"
                        size="xs"
                        disabled={
                          completeCommitment.isPending || completingId !== null
                        }
                        onClick={() => startComplete(c)}
                      >
                        Complete
                      </Button>
                    )}
                  </div>
                  {isCompleting && (
                    <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-end">
                      <div className="flex-1">
                        <Label
                          htmlFor={`commitment-note-${c._id}`}
                          className="text-xs"
                        >
                          Completion note (optional)
                        </Label>
                        <Input
                          id={`commitment-note-${c._id}`}
                          autoFocus
                          value={completionNote}
                          onChange={(e) => setCompletionNote(e.target.value)}
                          placeholder="What happened?"
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              confirmComplete(c);
                            } else if (e.key === "Escape") {
                              e.preventDefault();
                              cancelComplete();
                            }
                          }}
                        />
                      </div>
                      <div className="flex gap-2">
                        <Button
                          size="xs"
                          disabled={completeCommitment.isPending}
                          onClick={() => confirmComplete(c)}
                        >
                          Confirm
                        </Button>
                        <Button
                          variant="outline"
                          size="xs"
                          disabled={completeCommitment.isPending}
                          onClick={cancelComplete}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="space-y-2">
        <button
          type="button"
          onClick={() => setShowCompleted((v) => !v)}
          aria-expanded={showCompleted}
          className="flex items-center gap-2 text-sm font-medium hover:underline"
        >
          <span>{showCompleted ? "▾" : "▸"}</span>
          Recently completed{" "}
          <span className="text-muted-foreground">({completed.length})</span>
        </button>
        {showCompleted && completed.length > 0 && (
          <ul className="rounded-md border divide-y">
            {completed.map((c) => (
              <li key={c._id} className="px-3 py-2">
                <div className="text-sm line-through text-muted-foreground">
                  {c.content}
                </div>
                <div className="text-xs text-muted-foreground">
                  Completed{" "}
                  {c.completed_at ? timeAgo(c.completed_at) : "—"}
                  {c.completion_note && (
                    <span> — {c.completion_note}</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
        {showCompleted && completed.length === 0 && (
          <div className="rounded-md border border-dashed px-4 py-4 text-center text-sm text-muted-foreground">
            No completed commitments yet.
          </div>
        )}
      </section>
    </div>
  );
}
