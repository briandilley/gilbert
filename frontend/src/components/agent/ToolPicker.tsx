/**
 * ToolPicker — pick which tools are exposed to an agent at run time.
 *
 * Mirrors the backend mutex: ``tools_include`` and ``tools_exclude`` are
 * mutually exclusive, and both ``null`` means "all available tools at
 * the moment the run starts." The UI surfaces this as three modes via
 * tabs:
 *
 * - **All** — both lists are ``null``. The agent inherits whatever the
 *   owner has access to. If the owner loses a tool, the agent loses it
 *   too. This is the recommended default.
 * - **Include** — ``tools_include`` is an array (possibly empty), and
 *   ``tools_exclude`` is ``null``. The agent gets exactly the listed
 *   tools, plus the always-on core set.
 * - **Exclude** — ``tools_exclude`` is an array (possibly empty), and
 *   ``tools_include`` is ``null``. The agent gets everything the owner
 *   has *except* the listed tools. Core tools cannot be excluded.
 *
 * Tools are grouped by ``provider`` (alphabetical) using ``<details>``
 * collapsible sections, matching the existing pattern in
 * ``components/roles/ToolPermissions.tsx``.
 */

import { useMemo } from "react";
import { useAvailableTools } from "@/api/agents";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import type { ToolDescriptor } from "@/types/agent";

/**
 * Mirror of ``_CORE_AGENT_TOOLS`` in the backend
 * (``src/gilbert/core/services/agent.py``). Core tools are always
 * exposed and rendered checked-and-disabled regardless of the active
 * mode — removing them would break the agent's ability to complete a
 * run, ask for input, manage commitments, peer-message, or post in a
 * goal war room.
 */
const CORE_TOOLS = new Set<string>([
  // Phase 1A — agent self-management
  "complete_run",
  "request_user_input",
  "notify_user",
  "commitment_create",
  "commitment_complete",
  "commitment_list",
  "agent_memory_save",
  "agent_memory_search",
  "agent_memory_review_and_promote",
  // Phase 2 — peer messaging
  "agent_list",
  "agent_send_message",
  "agent_delegate",
  // Phase 4 — multi-agent goals (war-room post)
  "goal_post",
]);

type Mode = "all" | "include" | "exclude";

interface Props {
  toolsInclude: string[] | null;
  toolsExclude: string[] | null;
  onChange: (next: {
    tools_include: string[] | null;
    tools_exclude: string[] | null;
  }) => void;
}

interface ProviderGroup {
  provider: string;
  tools: ToolDescriptor[];
}

function groupByProvider(tools: ToolDescriptor[]): ProviderGroup[] {
  const map = new Map<string, ToolDescriptor[]>();
  for (const t of tools) {
    const key = t.provider || "(unspecified)";
    const arr = map.get(key) ?? [];
    arr.push(t);
    map.set(key, arr);
  }
  return [...map.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([provider, ts]) => ({ provider, tools: ts }));
}

function deriveMode(
  toolsInclude: string[] | null,
  toolsExclude: string[] | null,
): Mode {
  if (toolsInclude !== null) return "include";
  if (toolsExclude !== null) return "exclude";
  return "all";
}

export function ToolPicker({
  toolsInclude,
  toolsExclude,
  onChange,
}: Props) {
  const toolsQuery = useAvailableTools();
  const mode = deriveMode(toolsInclude, toolsExclude);

  const groups = useMemo(
    () => (toolsQuery.data ? groupByProvider(toolsQuery.data) : []),
    [toolsQuery.data],
  );

  const includeSet = useMemo(
    () => new Set<string>(toolsInclude ?? []),
    [toolsInclude],
  );
  const excludeSet = useMemo(
    () => new Set<string>(toolsExclude ?? []),
    [toolsExclude],
  );

  const handleTabChange = (next: Mode) => {
    if (next === mode) return;
    if (next === "all") {
      onChange({ tools_include: null, tools_exclude: null });
    } else if (next === "include") {
      // Seed with empty array — "only core tools."
      onChange({ tools_include: [], tools_exclude: null });
    } else {
      // Seed with empty array — "all owner tools, none excluded."
      onChange({ tools_include: null, tools_exclude: [] });
    }
  };

  const handleIncludeToggle = (name: string) => {
    if (CORE_TOOLS.has(name)) return;
    const current = toolsInclude ?? [];
    const next = current.includes(name)
      ? current.filter((n) => n !== name)
      : [...current, name];
    onChange({ tools_include: next, tools_exclude: null });
  };

  const handleExcludeToggle = (name: string) => {
    if (CORE_TOOLS.has(name)) return;
    const current = toolsExclude ?? [];
    // Checked = NOT in exclude list. Toggling means flipping membership.
    const next = current.includes(name)
      ? current.filter((n) => n !== name)
      : [...current, name];
    onChange({ tools_include: null, tools_exclude: next });
  };

  if (toolsQuery.isPending) {
    return <LoadingSpinner text="Loading tools…" />;
  }

  if (!toolsQuery.data || toolsQuery.data.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">
        No tools registered.
      </div>
    );
  }

  return (
    <Tabs
      value={mode}
      onValueChange={(v) => handleTabChange(String(v) as Mode)}
      className="w-full"
    >
      <TabsList>
        <TabsTrigger value="all">All</TabsTrigger>
        <TabsTrigger value="include">Include</TabsTrigger>
        <TabsTrigger value="exclude">Exclude</TabsTrigger>
      </TabsList>

      <TabsContent value="all">
        <p className="text-sm text-muted-foreground px-1 py-2">
          All tools available to you are exposed to this agent. If you lose
          access to a tool, the agent loses it too.
        </p>
      </TabsContent>

      <TabsContent value="include">
        <p className="text-sm text-muted-foreground px-1 py-2">
          Only the checked tools are exposed to this agent. Core tools are
          always included.
        </p>
        <ToolList
          groups={groups}
          isChecked={(name) =>
            CORE_TOOLS.has(name) || includeSet.has(name)
          }
          isDisabled={(name) => CORE_TOOLS.has(name)}
          onToggle={handleIncludeToggle}
        />
      </TabsContent>

      <TabsContent value="exclude">
        <p className="text-sm text-muted-foreground px-1 py-2">
          Every tool except the unchecked ones is exposed to this agent.
          Core tools cannot be excluded.
        </p>
        <ToolList
          groups={groups}
          isChecked={(name) =>
            CORE_TOOLS.has(name) || !excludeSet.has(name)
          }
          isDisabled={(name) => CORE_TOOLS.has(name)}
          onToggle={handleExcludeToggle}
        />
      </TabsContent>
    </Tabs>
  );
}

interface ToolListProps {
  groups: ProviderGroup[];
  isChecked: (name: string) => boolean;
  isDisabled: (name: string) => boolean;
  onToggle: (name: string) => void;
}

function ToolList({ groups, isChecked, isDisabled, onToggle }: ToolListProps) {
  return (
    <div className="space-y-2">
      {groups.map((group) => (
        <details
          key={group.provider}
          open
          className="rounded-md border"
        >
          <summary className="cursor-pointer select-none px-3 py-2 text-sm font-medium">
            {group.provider}{" "}
            <span className="text-muted-foreground font-normal">
              ({group.tools.length})
            </span>
          </summary>
          <ul className="px-3 pb-3 space-y-1">
            {group.tools.map((tool) => {
              const isCore = CORE_TOOLS.has(tool.name);
              const checked = isChecked(tool.name);
              const disabled = isDisabled(tool.name);
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
                    onChange={() => onToggle(tool.name)}
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
  );
}
