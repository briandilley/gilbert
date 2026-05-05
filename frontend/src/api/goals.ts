/**
 * Multi-agent goal API client (Phase 4) — typed React Query hooks
 * over the ``goals.*`` WS RPC namespace.
 *
 * Conventions mirror ``agents.ts``:
 * - Reads use ``useQuery`` with stable composite keys.
 * - Writes use ``useMutation`` and invalidate the relevant query
 *   keys on success.
 * - WS RPC frames go through ``useWebSocket().rpc``.
 *
 * Query keys:
 * - ``["goals", "list", ownerUserId | null]``
 * - ``["goals", "detail", goalId]``
 * - ``["goals", "summary", goalId]``
 * - ``["goals", "assignments", goalId | null, agentId | null, activeOnly]``
 * - ``["goals", "posts", goalId, limit | null]``
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import type {
  AssignmentRole,
  Goal,
  GoalAssignment,
  GoalStatus,
  GoalSummary,
  WarRoomPost,
} from "@/types/agent";

// ── Reads ─────────────────────────────────────────────────────────

export function useGoals(ownerUserId?: string) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["goals", "list", ownerUserId ?? null],
    queryFn: () =>
      rpc<{ goals: Goal[] }>({
        type: "goals.list",
        ...(ownerUserId ? { owner_user_id: ownerUserId } : {}),
      }).then((r) => r.goals),
    enabled: connected,
  });
}

export function useGoal(goalId: string | null | undefined) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["goals", "detail", goalId],
    queryFn: () =>
      rpc<{ goal: Goal }>({
        type: "goals.get",
        goal_id: goalId,
      }).then((r) => r.goal),
    enabled: connected && Boolean(goalId),
  });
}

export function useGoalSummary(goalId: string | null | undefined) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["goals", "summary", goalId],
    queryFn: () =>
      rpc<GoalSummary>({
        type: "goals.summary",
        goal_id: goalId,
      }),
    enabled: connected && Boolean(goalId),
  });
}

export function useGoalAssignments(
  goalId: string | null | undefined,
  options?: { agentId?: string | null; activeOnly?: boolean },
) {
  const { rpc, connected } = useWebSocket();
  const agentId = options?.agentId ?? null;
  const activeOnly = options?.activeOnly ?? true;
  return useQuery({
    queryKey: [
      "goals",
      "assignments",
      goalId ?? null,
      agentId,
      activeOnly,
    ],
    queryFn: () =>
      rpc<{ assignments: GoalAssignment[] }>({
        type: "goals.assignments.list",
        ...(goalId ? { goal_id: goalId } : {}),
        ...(agentId ? { agent_id: agentId } : {}),
        active_only: activeOnly,
      }).then((r) => r.assignments),
    enabled: connected && (Boolean(goalId) || Boolean(agentId)),
  });
}

export function useGoalPosts(
  goalId: string | null | undefined,
  limit?: number,
) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["goals", "posts", goalId, limit ?? null],
    queryFn: () =>
      rpc<{ posts: WarRoomPost[] }>({
        type: "goals.posts.list",
        goal_id: goalId,
        ...(limit ? { limit } : {}),
      }).then((r) => r.posts),
    enabled: connected && Boolean(goalId),
  });
}

// ── Writes ────────────────────────────────────────────────────────

export interface CreateGoalPayload {
  name: string;
  description?: string;
  cost_cap_usd?: number | null;
  /** Peer agent names to assign. First entry defaults to DRIVER. */
  assign_to?: string[];
}

export function useCreateGoal() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateGoalPayload) =>
      rpc<{ goal: Goal }>({
        type: "goals.create",
        name: payload.name,
        ...(payload.description !== undefined
          ? { description: payload.description }
          : {}),
        ...(payload.cost_cap_usd !== undefined
          ? { cost_cap_usd: payload.cost_cap_usd }
          : {}),
        ...(payload.assign_to ? { assign_to: payload.assign_to } : {}),
      }).then((r) => r.goal),
    onSuccess: (goal) => {
      qc.setQueryData(["goals", "detail", goal._id], goal);
      qc.invalidateQueries({ queryKey: ["goals", "list"] });
    },
  });
}

export function useUpdateGoalStatus() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      goalId,
      status,
    }: {
      goalId: string;
      status: GoalStatus;
    }) =>
      rpc<{ goal: Goal }>({
        type: "goals.update_status",
        goal_id: goalId,
        status,
      }).then((r) => r.goal),
    onSuccess: (goal) => {
      qc.setQueryData(["goals", "detail", goal._id], goal);
      qc.invalidateQueries({ queryKey: ["goals", "list"] });
      qc.invalidateQueries({ queryKey: ["goals", "detail", goal._id] });
      qc.invalidateQueries({ queryKey: ["goals", "summary", goal._id] });
    },
  });
}

export function useAssignAgentToGoal() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      goalId,
      agentId,
      role,
    }: {
      goalId: string;
      agentId: string;
      role: AssignmentRole;
    }) =>
      rpc<{ assignment: GoalAssignment }>({
        type: "goals.assignments.add",
        goal_id: goalId,
        agent_id: agentId,
        role,
      }).then((r) => r.assignment),
    onSuccess: (assignment) => {
      qc.invalidateQueries({
        queryKey: ["goals", "assignments", assignment.goal_id],
      });
      qc.invalidateQueries({
        queryKey: ["goals", "summary", assignment.goal_id],
      });
      qc.invalidateQueries({ queryKey: ["goals", "list"] });
    },
  });
}

export function useUnassignAgent() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      goalId,
      agentId,
    }: {
      goalId: string;
      agentId: string;
    }) =>
      rpc<{ assignment: GoalAssignment }>({
        type: "goals.assignments.remove",
        goal_id: goalId,
        agent_id: agentId,
      }).then((r) => r.assignment),
    onSuccess: (assignment) => {
      qc.invalidateQueries({
        queryKey: ["goals", "assignments", assignment.goal_id],
      });
      qc.invalidateQueries({
        queryKey: ["goals", "summary", assignment.goal_id],
      });
      qc.invalidateQueries({ queryKey: ["goals", "list"] });
    },
  });
}

export function useHandoffGoal() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      goalId,
      fromAgentId,
      toAgentId,
      newRoleForFrom,
      note,
    }: {
      goalId: string;
      fromAgentId: string;
      toAgentId: string;
      newRoleForFrom?: AssignmentRole;
      note?: string;
    }) =>
      rpc<{
        from_assignment: GoalAssignment;
        to_assignment: GoalAssignment;
      }>({
        type: "goals.assignments.handoff",
        goal_id: goalId,
        from_agent_id: fromAgentId,
        to_agent_id: toAgentId,
        ...(newRoleForFrom ? { new_role_for_from: newRoleForFrom } : {}),
        ...(note ? { note } : {}),
      }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({
        queryKey: ["goals", "assignments", vars.goalId],
      });
      qc.invalidateQueries({
        queryKey: ["goals", "summary", vars.goalId],
      });
      qc.invalidateQueries({ queryKey: ["goals", "list"] });
    },
  });
}
