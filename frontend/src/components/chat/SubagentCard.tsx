import { CheckIcon, CircleXIcon, EyeIcon, LoaderIcon, SquareIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ActiveSubagent } from "@/types/events";

const STATUS_LABEL: Record<ActiveSubagent["status"], string> = {
  running: "Running",
  completed: "Done",
  failed: "Failed",
  stopped: "Stopped",
};

export function SubagentCard({
  subagent,
  onWatch,
  onStop,
}: {
  subagent: ActiveSubagent;
  /** Called when the user clicks Watch — opens the live viewer. */
  onWatch?: () => void;
  /** Called when the user clicks Stop — requests graceful stop. */
  onStop?: () => void;
}) {
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
      {subagent.query && (
        <span className="text-muted-foreground truncate max-w-[200px]">
          — {subagent.query}
        </span>
      )}
      <span className="text-muted-foreground">— {STATUS_LABEL[subagent.status]}</span>
      {failed && subagent.reason ? (
        <span className="text-rose-400 truncate">— {subagent.reason}</span>
      ) : null}
      <div className="ml-auto flex items-center gap-1">
        {running && subagent.conversationId && onWatch && (
          <button
            onClick={onWatch}
            className="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
            title="Watch subagent activity"
          >
            <EyeIcon className="size-3" />
            Watch
          </button>
        )}
        {running && onStop && (
          <button
            onClick={onStop}
            className="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:text-rose-400 hover:bg-muted transition-colors"
            title="Stop subagent"
          >
            <SquareIcon className="size-3" />
            Stop
          </button>
        )}
      </div>
    </div>
  );
}
