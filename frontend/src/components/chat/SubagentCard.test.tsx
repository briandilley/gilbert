import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ActiveSubagent } from "@/types/events";
import { SubagentCard } from "./SubagentCard";

const base: ActiveSubagent = {
  subagent_id: "a1",
  agent_type: "general-purpose",
  status: "running",
};

describe("SubagentCard", () => {
  it("shows the agent type and a running state", () => {
    render(<SubagentCard subagent={base} />);
    expect(screen.getByText(/general-purpose/i)).toBeInTheDocument();
    expect(screen.getByText(/running/i)).toBeInTheDocument();
  });

  it("shows completed state", () => {
    render(<SubagentCard subagent={{ ...base, status: "completed" }} />);
    expect(screen.getByText(/done|completed/i)).toBeInTheDocument();
  });

  it("shows the failure reason", () => {
    render(<SubagentCard subagent={{ ...base, status: "failed", reason: "boom" }} />);
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
    expect(screen.getByText(/boom/i)).toBeInTheDocument();
  });
});
