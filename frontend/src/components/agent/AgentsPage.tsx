// Stub — replaced in Phase 1B Task 11. The legacy goal-based UI has
// been removed; this file currently exists only to keep ``tsc -b`` and
// the App.tsx route wiring happy until the new agent UI lands.

export function AgentsPage() {
  return null;
}

export interface CreateGoalDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  profiles: Array<{ name: string; description?: string }>;
  onCreated?: () => void;
}

export function CreateGoalDialog(_: CreateGoalDialogProps) {
  return null;
}
