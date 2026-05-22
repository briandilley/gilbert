import { useCallback } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";
import { useAgents } from "@/api/agents";
import { useEventBus } from "@/hooks/useEventBus";
import { buttonVariants } from "@/components/ui/button";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { PageHeader } from "@/components/layout/PageHeader";
import { AgentCard } from "./AgentCard";

export function AgentsListPage() {
  const queryClient = useQueryClient();
  const { data: agents, isPending, isError } = useAgents();

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["agents", "list"] });
  }, [queryClient]);

  // Live updates — agents may be created/updated/deleted by other
  // sessions, and run completions can change ``updated_at`` /
  // ``lifetime_cost_usd`` on the existing card.
  useEventBus("agent.created", invalidate);
  useEventBus("agent.updated", invalidate);
  useEventBus("agent.deleted", invalidate);
  useEventBus("agent.run.completed", invalidate);

  const count = agents?.length ?? 0;

  return (
    <div>
      <PageHeader
        eyebrow="AUTONOMOUS"
        title="Agents"
        description={
          isPending
            ? "Loading…"
            : `${count} agent${count === 1 ? "" : "s"}.`
        }
        actions={
          <Link to="/agents/new" className={buttonVariants({ size: "sm" })}>
            <PlusIcon /> New agent
          </Link>
        }
      />

      <div className="mx-auto max-w-5xl px-4 py-4 sm:px-6 sm:py-6 space-y-3">
        {isPending && <LoadingSpinner text="Loading agents…" />}

        {isError && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            Failed to load agents.
          </div>
        )}

        {!isPending && !isError && agents && agents.length === 0 && (
          <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border py-16 text-center">
            <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
              No agents
            </p>
            <p className="max-w-md text-sm text-muted-foreground">
              Agents are durable AI personalities with their own
              memory, tools, and commitments. Create one to get started.
            </p>
            <Link to="/agents/new" className={buttonVariants({ size: "sm" })}>
              <PlusIcon /> New agent
            </Link>
          </div>
        )}

        {!isPending && !isError && agents && agents.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {agents.map((agent) => (
              <AgentCard key={agent._id} agent={agent} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
