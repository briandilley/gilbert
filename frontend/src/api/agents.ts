/**
 * Agent-domain API client — typed React Query hooks over the
 * ``agents.*`` WS RPC namespace, plus a plain async helper for the
 * multipart avatar upload HTTP route.
 *
 * Conventions:
 * - Reads use ``useQuery`` and read-side query keys consistent across
 *   the codebase (see below).
 * - Writes use ``useMutation`` and invalidate the relevant query keys
 *   on success.
 * - WS RPC frames are sent through ``useWebSocket().rpc``; HTTP
 *   uploads use ``fetch`` directly so the browser can set the
 *   ``multipart/form-data`` boundary header (the shared ``apiFetch``
 *   helper forces ``application/json``, which would break multipart).
 *
 * Query keys:
 * - ``["agents", "list"]``
 * - ``["agents", "detail", agentId]``
 * - ``["agents", "defaults"]``
 * - ``["agents", "runs", agentId]``
 * - ``["agents", "commitments", agentId, includeCompleted]``
 * - ``["agents", "memories", agentId, filters]``
 * - ``["agents", "tools-available"]``
 * - ``["agents", "tool-groups"]``
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import { ApiError } from "@/api/client";
import type {
  Agent,
  AgentCreatePayload,
  AgentDefaults,
  AgentMemory,
  AgentRun,
  AgentStatus,
  AgentUpdatePayload,
  Commitment,
  MemoryFilters,
  MemoryState,
  ToolDescriptor,
  ToolGroupMap,
} from "@/types/agent";

// ── Reads ─────────────────────────────────────────────────────────

export function useAgents(ownerUserId?: string) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "list", ownerUserId ?? null],
    queryFn: () =>
      rpc<{ agents: Agent[] }>({
        type: "agents.list",
        ...(ownerUserId ? { owner_user_id: ownerUserId } : {}),
      }).then((r) => r.agents),
    enabled: connected,
  });
}

export function useAgent(agentId: string | null | undefined) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "detail", agentId],
    queryFn: () =>
      rpc<{ agent: Agent }>({
        type: "agents.get",
        agent_id: agentId,
      }).then((r) => r.agent),
    enabled: connected && Boolean(agentId),
  });
}

export function useAgentDefaults() {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "defaults"],
    queryFn: () =>
      rpc<{ defaults: AgentDefaults }>({ type: "agents.get_defaults" }).then(
        (r) => r.defaults,
      ),
    enabled: connected,
  });
}

export function useAgentRuns(
  agentId: string | null | undefined,
  limit?: number,
) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "runs", agentId, limit ?? null],
    queryFn: () =>
      rpc<{ runs: AgentRun[] }>({
        type: "agents.runs.list",
        agent_id: agentId,
        ...(limit ? { limit } : {}),
      }).then((r) => r.runs),
    enabled: connected && Boolean(agentId),
  });
}

export function useAgentCommitments(
  agentId: string | null | undefined,
  includeCompleted = false,
) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "commitments", agentId, includeCompleted],
    queryFn: () =>
      rpc<{ commitments: Commitment[] }>({
        type: "agents.commitments.list",
        agent_id: agentId,
        include_completed: includeCompleted,
      }).then((r) => r.commitments),
    enabled: connected && Boolean(agentId),
  });
}

export function useAgentMemories(
  agentId: string | null | undefined,
  filters: MemoryFilters = {},
) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "memories", agentId, filters],
    queryFn: () =>
      rpc<{ memories: AgentMemory[] }>({
        type: "agents.memories.list",
        agent_id: agentId,
        ...(filters.state ? { state: filters.state } : {}),
        ...(filters.kind ? { kind: filters.kind } : {}),
        ...(filters.tags ? { tags: filters.tags } : {}),
        ...(filters.q ? { q: filters.q } : {}),
        ...(filters.limit ? { limit: filters.limit } : {}),
      }).then((r) => r.memories),
    enabled: connected && Boolean(agentId),
  });
}

export function useAvailableTools() {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "tools-available"],
    queryFn: () =>
      rpc<{ tools: ToolDescriptor[] }>({
        type: "agents.tools.list_available",
      }).then((r) => r.tools),
    enabled: connected,
  });
}

export function useToolGroups() {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["agents", "tool-groups"],
    queryFn: () =>
      rpc<{ groups: ToolGroupMap }>({
        type: "agents.tools.list_groups",
      }).then((r) => r.groups),
    enabled: connected,
  });
}

// ── Writes ────────────────────────────────────────────────────────

export function useCreateAgent() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: AgentCreatePayload) =>
      rpc<{ agent: Agent }>({ type: "agents.create", ...payload }).then(
        (r) => r.agent,
      ),
    onSuccess: (agent) => {
      qc.invalidateQueries({ queryKey: ["agents", "list"] });
      qc.setQueryData(["agents", "detail", agent._id], agent);
    },
  });
}

export function useUpdateAgent() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      patch,
    }: {
      agentId: string;
      patch: AgentUpdatePayload;
    }) =>
      rpc<{ agent: Agent }>({
        type: "agents.update",
        agent_id: agentId,
        patch,
      }).then((r) => r.agent),
    onSuccess: (agent) => {
      qc.invalidateQueries({ queryKey: ["agents", "list"] });
      qc.setQueryData(["agents", "detail", agent._id], agent);
    },
  });
}

export function useDeleteAgent() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (agentId: string) =>
      rpc<{ deleted: boolean }>({
        type: "agents.delete",
        agent_id: agentId,
      }),
    onSuccess: (_, agentId) => {
      qc.invalidateQueries({ queryKey: ["agents", "list"] });
      qc.removeQueries({ queryKey: ["agents", "detail", agentId] });
      qc.removeQueries({ queryKey: ["agents", "runs", agentId] });
      qc.removeQueries({ queryKey: ["agents", "commitments", agentId] });
      qc.removeQueries({ queryKey: ["agents", "memories", agentId] });
    },
  });
}

export function useSetAgentStatus() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      status,
    }: {
      agentId: string;
      status: AgentStatus;
    }) =>
      rpc<{ agent: Agent }>({
        type: "agents.set_status",
        agent_id: agentId,
        status,
      }).then((r) => r.agent),
    onSuccess: (agent) => {
      qc.invalidateQueries({ queryKey: ["agents", "list"] });
      qc.setQueryData(["agents", "detail", agent._id], agent);
    },
  });
}

export function useRunAgentNow() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      userMessage,
    }: {
      agentId: string;
      userMessage?: string;
    }) =>
      rpc<{ run_id: string; status: string }>({
        type: "agents.run_now",
        agent_id: agentId,
        ...(userMessage ? { user_message: userMessage } : {}),
      }),
    onSuccess: (_, { agentId }) => {
      qc.invalidateQueries({ queryKey: ["agents", "runs", agentId] });
      qc.invalidateQueries({ queryKey: ["agents", "detail", agentId] });
    },
  });
}

export function useCreateCommitment() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      content,
      dueAt,
      dueInSeconds,
    }: {
      agentId: string;
      content: string;
      dueAt?: string;
      dueInSeconds?: number;
    }) =>
      rpc<{ commitment: Commitment }>({
        type: "agents.commitments.create",
        agent_id: agentId,
        content,
        ...(dueAt ? { due_at: dueAt } : {}),
        ...(dueInSeconds !== undefined ? { due_in_seconds: dueInSeconds } : {}),
      }).then((r) => r.commitment),
    onSuccess: (commitment) => {
      qc.invalidateQueries({
        queryKey: ["agents", "commitments", commitment.agent_id],
      });
    },
  });
}

export function useCompleteCommitment() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      commitmentId,
      note,
    }: {
      commitmentId: string;
      note?: string;
    }) =>
      rpc<{ commitment: Commitment }>({
        type: "agents.commitments.complete",
        commitment_id: commitmentId,
        ...(note ? { note } : {}),
      }).then((r) => r.commitment),
    onSuccess: (commitment) => {
      qc.invalidateQueries({
        queryKey: ["agents", "commitments", commitment.agent_id],
      });
    },
  });
}

export function useSetMemoryState() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      memoryId,
      state,
    }: {
      memoryId: string;
      state: MemoryState;
    }) =>
      rpc<{ memory: AgentMemory }>({
        type: "agents.memories.set_state",
        memory_id: memoryId,
        state,
      }).then((r) => r.memory),
    onSuccess: (memory) => {
      qc.invalidateQueries({
        queryKey: ["agents", "memories", memory.agent_id],
      });
    },
  });
}

// ── HTTP avatar upload ────────────────────────────────────────────

/**
 * Upload an image avatar via the multipart HTTP route. The shared
 * ``apiFetch`` helper forces ``Content-Type: application/json`` which
 * would clobber the multipart boundary, so we use ``fetch`` directly
 * and let the browser set the header. The session cookie rides along
 * via ``credentials: "include"``.
 */
export async function uploadAgentAvatar(
  agentId: string,
  file: File,
): Promise<Agent> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/avatar`, {
    method: "POST",
    body: form,
    credentials: "include",
  });
  if (res.status === 401) {
    window.location.href = "/auth/login";
    throw new ApiError(401, "Unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      detail = body.detail || detail;
    } catch {
      // ignore
    }
    throw new ApiError(res.status, detail);
  }
  const body = (await res.json()) as { agent: Agent };
  return body.agent;
}
