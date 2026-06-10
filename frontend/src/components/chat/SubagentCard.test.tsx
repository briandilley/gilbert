import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ActiveSubagent } from "@/types/events";
import { SubagentCard } from "./SubagentCard";

const base: ActiveSubagent = {
  subagent_id: "a1",
  agent_type: "general-purpose",
  status: "running",
  conversationId: "sub-conv-1",
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

  it("shows Watch and Stop buttons when running", () => {
    const onWatch = vi.fn();
    const onStop = vi.fn();
    render(<SubagentCard subagent={base} onWatch={onWatch} onStop={onStop} />);
    const watchBtn = screen.getByText(/watch/i);
    const stopBtn = screen.getByText(/stop/i);
    expect(watchBtn).toBeInTheDocument();
    expect(stopBtn).toBeInTheDocument();
    fireEvent.click(watchBtn);
    expect(onWatch).toHaveBeenCalledOnce();
    fireEvent.click(stopBtn);
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("does not show Watch/Stop when not running", () => {
    render(
      <SubagentCard
        subagent={{ ...base, status: "completed" }}
        onWatch={() => {}}
        onStop={() => {}}
      />,
    );
    expect(screen.queryByText(/watch/i)).toBeNull();
    expect(screen.queryByText(/stop/i)).toBeNull();
  });
});
