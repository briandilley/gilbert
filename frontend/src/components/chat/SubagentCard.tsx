import { CheckIcon, CircleXIcon, LoaderIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ActiveSubagent } from "@/types/events";

const STATUS_LABEL: Record<ActiveSubagent["status"], string> = {
  running: "Running",
  completed: "Done",
  failed: "Failed",
};

export function SubagentCard({ subagent }: { subagent: ActiveSubagent }) {
  const running = subagent.status === "running";
  const failed = subagent.status === "failed";
  const Icon = running ? LoaderIcon : failed ? CircleXIcon : CheckIcon;
  return (
    <div
      className={cn(
        "w-full max-w-2xl rounded-md border border-border bg-card/40 my-2 px-3 py-1.5",
        "flex items-center gap-2 text-xs",
        running && "border-dashed border-(--signal)/40 animate-pulse",
        failed && "border-rose-500/40",
      )}
    >
      <Icon className={cn("size-3 shrink-0", running && "animate-spin")} />
      <span className="font-medium">Subagent: {subagent.agent_type}</span>
      <span className="text-muted-foreground">— {STATUS_LABEL[subagent.status]}</span>
      {failed && subagent.reason ? (
        <span className="text-rose-400 truncate">— {subagent.reason}</span>
      ) : null}
    </div>
  );
}
