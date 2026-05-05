import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { timeAgo } from "@/lib/timeAgo";
import { AgentAvatar } from "./AgentAvatar";
import type { Agent } from "@/types/agent";

function formatCost(agent: Agent): string {
  const lifetime = `$${agent.lifetime_cost_usd.toFixed(2)}`;
  if (agent.cost_cap_usd != null) {
    return `${lifetime} / $${agent.cost_cap_usd.toFixed(2)}`;
  }
  return lifetime;
}

interface Props {
  agent: Agent;
}

/**
 * Compact card summarising an agent — used by the agents list page.
 * The whole card is a link to the detail page. "Last active" is
 * approximated as ``updated_at`` to avoid a per-card runs query;
 * surfacing the actual last-run timestamp is a follow-up.
 */
export function AgentCard({ agent }: Props) {
  const isEnabled = agent.status === "enabled";
  return (
    <Link
      to={`/agents/${encodeURIComponent(agent._id)}`}
      className="block transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-xl"
    >
      <Card>
        <CardContent>
          <div className="flex items-start gap-3">
            <AgentAvatar agent={agent} size="md" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="font-medium truncate">{agent.name}</div>
                  {agent.role_label && (
                    <div className="text-xs text-muted-foreground truncate">
                      {agent.role_label}
                    </div>
                  )}
                </div>
                <Badge
                  className={
                    isEnabled
                      ? "bg-green-500/15 text-green-600 dark:text-green-400"
                      : "bg-yellow-500/15 text-yellow-600 dark:text-yellow-400"
                  }
                  variant="outline"
                >
                  {agent.status}
                </Badge>
              </div>
              <div className="mt-2 space-y-0.5 text-xs text-muted-foreground">
                <div>Last active: {timeAgo(agent.updated_at)}</div>
                <div>Cost: {formatCost(agent)}</div>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
