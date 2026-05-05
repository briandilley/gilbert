import { useCallback } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";
import { useAgents } from "@/api/agents";
import { useEventBus } from "@/hooks/useEventBus";
import { buttonVariants } from "@/components/ui/button";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
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

  return (
    <div className="p-4 sm:p-6 space-y-4 max-w-5xl mx-auto">
      <div className="flex items-center justify-between gap-2">
        <h1 className="text-xl sm:text-2xl font-semibold">Agents</h1>
        <Link to="/agents/new" className={buttonVariants()}>
          <PlusIcon /> New agent
        </Link>
      </div>

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
        <div className="rounded-md border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
          No agents yet — click "New agent" to create one.
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
  );
}
