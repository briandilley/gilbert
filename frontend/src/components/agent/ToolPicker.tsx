import { useMemo } from "react";
import { useAvailableTools, useToolGroups } from "@/api/agents";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import type { ToolDescriptor } from "@/types/agent";

/**
 * Mirror of ``_CORE_AGENT_TOOLS`` in the backend. Core tools are always
 * allowed and rendered checked-and-disabled — removing them from the
 * allowlist would break the agent's ability to complete a run, ask for
 * input, manage commitments, or persist memories.
 */
const CORE_TOOLS = new Set<string>([
  "complete_run",
  "request_user_input",
  "notify_user",
  "commitment_create",
  "commitment_complete",
  "commitment_list",
  "agent_memory_save",
  "agent_memory_search",
  "agent_memory_review_and_promote",
]);

interface Props {
  /** ``null`` means "all tools allowed". An array means "only these tools". */
  value: string[] | null;
  onChange: (next: string[] | null) => void;
}

interface OrganizedGroup {
  name: string;
  tools: ToolDescriptor[];
}

/**
 * Organize tools into groups: each named group from ``useToolGroups()``
 * appears in declared order, and any tool not assigned to a group falls
 * into a synthesized "Other" bucket.
 */
function organizeTools(
  tools: ToolDescriptor[],
  groups: Record<string, string[]>,
): OrganizedGroup[] {
  const byName = new Map<string, ToolDescriptor>();
  for (const t of tools) byName.set(t.name, t);

  const claimed = new Set<string>();
  const result: OrganizedGroup[] = [];
  for (const [groupName, toolNames] of Object.entries(groups)) {
    const groupTools: ToolDescriptor[] = [];
    for (const n of toolNames) {
      const t = byName.get(n);
      if (t) {
        groupTools.push(t);
        claimed.add(n);
      }
    }
    if (groupTools.length > 0) {
      result.push({ name: groupName, tools: groupTools });
    }
  }

  const other: ToolDescriptor[] = [];
  for (const t of tools) {
    if (!claimed.has(t.name)) other.push(t);
  }
  if (other.length > 0) result.push({ name: "Other", tools: other });
  return result;
}

export function ToolPicker({ value, onChange }: Props) {
  const toolsQuery = useAvailableTools();
  const groupsQuery = useToolGroups();

  const allAllowed = value === null;
  const selected = useMemo(
    () => new Set<string>(value ?? []),
    [value],
  );

  const organized = useMemo(() => {
    if (!toolsQuery.data || !groupsQuery.data) return [];
    return organizeTools(toolsQuery.data, groupsQuery.data);
  }, [toolsQuery.data, groupsQuery.data]);

  if (toolsQuery.isPending || groupsQuery.isPending) {
    return <LoadingSpinner text="Loading tools…" />;
  }

  if (!toolsQuery.data || toolsQuery.data.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">
        No tools registered.
      </div>
    );
  }

  const handleToggleAllAllowed = (next: boolean) => {
    if (next) {
      onChange(null);
    } else {
      onChange(Array.from(CORE_TOOLS));
    }
  };

  const handleToggleTool = (name: string) => {
    if (allAllowed) return;
    if (CORE_TOOLS.has(name)) return;
    const current = value ?? [];
    if (current.includes(name)) {
      onChange(current.filter((n) => n !== name));
    } else {
      onChange([...current, name]);
    }
  };

  return (
    <div className="space-y-3">
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={allAllowed}
          onChange={(e) => handleToggleAllAllowed(e.target.checked)}
        />
        <span className="font-medium">All tools allowed</span>
        <span className="text-muted-foreground text-xs">
          (uncheck to restrict to a specific list)
        </span>
      </label>

      <div className="space-y-2">
        {organized.map((group) => (
          <details key={group.name} open className="rounded-md border">
            <summary className="cursor-pointer select-none px-3 py-2 text-sm font-medium">
              {group.name}{" "}
              <span className="text-muted-foreground font-normal">
                ({group.tools.length})
              </span>
            </summary>
            <ul className="px-3 pb-3 space-y-1">
              {group.tools.map((tool) => {
                const isCore = CORE_TOOLS.has(tool.name);
                const checked =
                  allAllowed || isCore || selected.has(tool.name);
                const disabled = allAllowed || isCore;
                return (
                  <li
                    key={tool.name}
                    className="flex items-start gap-2 text-sm"
                  >
                    <input
                      id={`toolpicker-${tool.name}`}
                      type="checkbox"
                      className="mt-1"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => handleToggleTool(tool.name)}
                    />
                    <label
                      htmlFor={`toolpicker-${tool.name}`}
                      className="flex-1"
                    >
                      <div className="font-mono text-xs">
                        {tool.name}
                        {isCore && (
                          <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-[0.65rem] uppercase tracking-wide text-muted-foreground">
                            core
                          </span>
                        )}
                      </div>
                      {tool.description && (
                        <div className="text-muted-foreground text-xs">
                          {tool.description}
                        </div>
                      )}
                    </label>
                  </li>
                );
              })}
            </ul>
          </details>
        ))}
      </div>
    </div>
  );
}
