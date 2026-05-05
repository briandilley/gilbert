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
 * - ``["goals", "deliverables", goalId | null, state | null]``
 * - ``["goals", "dependencies", dependentGoalId | null, sourceGoalId | null, satisfied | null]``
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import type {
  AssignmentRole,
  Deliverable,
  DeliverableState,
  Goal,
  GoalAssignment,
  GoalDependency,
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

export function useDeleteGoal() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ goalId }: { goalId: string }) =>
      rpc<{ deleted: boolean }>({
        type: "goals.delete",
        goal_id: goalId,
      }).then((r) => ({ goalId, deleted: r.deleted })),
    onSuccess: ({ goalId }) => {
      qc.removeQueries({ queryKey: ["goals", "detail", goalId] });
      qc.removeQueries({ queryKey: ["goals", "summary", goalId] });
      qc.invalidateQueries({ queryKey: ["goals", "list"] });
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

// ── Deliverables (Phase 5) ────────────────────────────────────────

export function useDeliverables(
  goalId: string | null | undefined,
  state?: DeliverableState,
) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: ["goals", "deliverables", goalId ?? null, state ?? null],
    queryFn: () =>
      rpc<{ deliverables: Deliverable[] }>({
        type: "deliverables.list",
        ...(goalId ? { goal_id: goalId } : {}),
        ...(state ? { state } : {}),
      }).then((r) => r.deliverables),
    enabled: connected && Boolean(goalId),
  });
}

export function useCreateDeliverable() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      goalId,
      name,
      kind,
      contentRef,
    }: {
      goalId: string;
      name: string;
      kind: string;
      contentRef?: string;
    }) =>
      rpc<{ deliverable: Deliverable }>({
        type: "deliverables.create",
        goal_id: goalId,
        name,
        kind,
        ...(contentRef !== undefined ? { content_ref: contentRef } : {}),
      }).then((r) => r.deliverable),
    onSuccess: (deliverable) => {
      qc.invalidateQueries({
        queryKey: ["goals", "deliverables", deliverable.goal_id],
      });
      qc.invalidateQueries({
        queryKey: ["goals", "summary", deliverable.goal_id],
      });
    },
  });
}

export function useFinalizeDeliverable() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (deliverableId: string) =>
      rpc<{ deliverable: Deliverable }>({
        type: "deliverables.finalize",
        deliverable_id: deliverableId,
      }).then((r) => r.deliverable),
    onSuccess: (deliverable) => {
      qc.invalidateQueries({
        queryKey: ["goals", "deliverables", deliverable.goal_id],
      });
      // Finalizing may unblock dependent goals — broadly invalidate
      // every goal summary.
      qc.invalidateQueries({ queryKey: ["goals", "summary"] });
    },
  });
}

export function useSupersedeDeliverable() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      deliverableId,
      newContentRef,
      finalize,
    }: {
      deliverableId: string;
      newContentRef: string;
      finalize?: boolean;
    }) =>
      rpc<{ old: Deliverable; new: Deliverable }>({
        type: "deliverables.supersede",
        deliverable_id: deliverableId,
        new_content_ref: newContentRef,
        ...(finalize !== undefined ? { finalize } : {}),
      }),
    onSuccess: (data) => {
      qc.invalidateQueries({
        queryKey: ["goals", "deliverables", data.new.goal_id],
      });
      // Supersede may finalize, which may unblock dependents.
      qc.invalidateQueries({ queryKey: ["goals", "summary"] });
    },
  });
}

// ── Goal dependencies (Phase 5) ───────────────────────────────────

export function useDependencies(
  dependentGoalId?: string | null,
  sourceGoalId?: string | null,
  satisfied?: boolean | null,
) {
  const { rpc, connected } = useWebSocket();
  return useQuery({
    queryKey: [
      "goals",
      "dependencies",
      dependentGoalId ?? null,
      sourceGoalId ?? null,
      satisfied ?? null,
    ],
    queryFn: () =>
      rpc<{ dependencies: GoalDependency[] }>({
        type: "goals.dependencies.list",
        ...(dependentGoalId ? { dependent_goal_id: dependentGoalId } : {}),
        ...(sourceGoalId ? { source_goal_id: sourceGoalId } : {}),
        ...(satisfied !== undefined && satisfied !== null
          ? { satisfied }
          : {}),
      }).then((r) => r.dependencies),
    enabled:
      connected && (Boolean(dependentGoalId) || Boolean(sourceGoalId)),
  });
}

export function useAddDependency() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      dependentGoalId,
      sourceGoalId,
      requiredDeliverableName,
    }: {
      dependentGoalId: string;
      sourceGoalId: string;
      requiredDeliverableName: string;
    }) =>
      rpc<{ dependency: GoalDependency }>({
        type: "goals.dependencies.add",
        dependent_goal_id: dependentGoalId,
        source_goal_id: sourceGoalId,
        required_deliverable_name: requiredDeliverableName,
      }).then((r) => r.dependency),
    onSuccess: (dependency) => {
      qc.invalidateQueries({ queryKey: ["goals", "dependencies"] });
      qc.invalidateQueries({
        queryKey: ["goals", "summary", dependency.dependent_goal_id],
      });
    },
  });
}

export function useRemoveDependency() {
  const { rpc } = useWebSocket();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (dependencyId: string) =>
      rpc<{ removed: boolean }>({
        type: "goals.dependencies.remove",
        dependency_id: dependencyId,
      }).then((r) => r.removed),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["goals", "dependencies"] });
      qc.invalidateQueries({ queryKey: ["goals", "summary"] });
    },
  });
}
