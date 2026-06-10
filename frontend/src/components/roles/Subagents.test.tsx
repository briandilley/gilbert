import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Subagents } from "./Subagents";
import type { SubagentTypeDTO } from "@/types/subagent";

// ── Mocks ──────────────────────────────────────────────────────────────────

vi.mock("@/hooks/useWebSocket", () => ({
  useWebSocket: () => ({ connected: true }),
}));

const mockSaveSubagentType = vi.fn().mockResolvedValue({ ok: true });
const mockDeleteSubagentType = vi.fn().mockResolvedValue({ ok: true });
const mockResetSubagentType = vi.fn().mockResolvedValue({ ok: true });
let mockListSubagentTypes: () => Promise<{ types: SubagentTypeDTO[]; all_tool_names: string[] }>;
let mockListModels: () => Promise<{ backends: { name: string; models: { id: string; name: string }[] }[] }>;

vi.mock("@/hooks/useWsApi", () => ({
  useWsApi: () => ({
    listSubagentTypes: () => mockListSubagentTypes(),
    saveSubagentType: (t: SubagentTypeDTO) => mockSaveSubagentType(t),
    deleteSubagentType: (id: string) => mockDeleteSubagentType(id),
    resetSubagentType: (id: string) => mockResetSubagentType(id),
    listModels: () => mockListModels(),
  }),
}));

// Stub the PageHeader so we don't need layout context — render actions too
// so tests can click the "New type" button that lives in actions.
vi.mock("@/components/layout/PageHeader", () => ({
  PageHeader: ({ title, actions }: { title: string; actions?: React.ReactNode }) => (
    <div>
      <h1>{title}</h1>
      {actions}
    </div>
  ),
}));

// ── Fixtures ───────────────────────────────────────────────────────────────

const builtInType: SubagentTypeDTO = {
  id: "deep-research",
  name: "Research Analyst",
  description: "Thorough research",
  system_prompt: "You are a research agent.",
  ai_profile: "",
  backend: "",
  model: "",
  temperature: 0.4,
  max_tokens: null,
  tool_mode: "include",
  tools: ["web_search", "fetch_url"],
  max_rounds: 40,
  max_wall_clock_s: 900,
  execution_mode: "background",
  deliver_as: "report_file",
  enabled: true,
  built_in: true,
  icon: "",
};

const customType: SubagentTypeDTO = {
  id: "my-agent",
  name: "My Agent",
  description: "Custom agent",
  system_prompt: "Do stuff.",
  ai_profile: "",
  backend: "",
  model: "",
  temperature: null,
  max_tokens: null,
  tool_mode: "all",
  tools: [],
  max_rounds: 12,
  max_wall_clock_s: 300,
  execution_mode: "sync",
  deliver_as: "inline",
  enabled: true,
  built_in: false,
  icon: "",
};

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function renderSubagents() {
  return render(
    <QueryClientProvider client={makeClient()}>
      <Subagents />
    </QueryClientProvider>,
  );
}

// ── Tests ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks();
  mockListSubagentTypes = () =>
    Promise.resolve({
      types: [builtInType, customType],
      all_tool_names: ["web_search", "fetch_url", "write_workspace_file"],
      all_profiles: ["standard", "fast"],
    });
  mockListModels = () =>
    Promise.resolve({ backends: [] });
});

describe("Subagents admin page", () => {
  it("renders cards from listSubagentTypes", async () => {
    renderSubagents();
    await waitFor(() => {
      expect(screen.getByText("Research Analyst")).toBeInTheDocument();
      expect(screen.getByText("My Agent")).toBeInTheDocument();
    });
  });

  it("built-in types show a Reset button and no Delete button", async () => {
    renderSubagents();
    await waitFor(() =>
      expect(screen.getByText("Research Analyst")).toBeInTheDocument(),
    );

    // There should be a Reset (RotateCcwIcon) for the built-in.
    // We identify buttons by their title attributes.
    const resetBtn = screen.getByTitle("Reset to defaults");
    expect(resetBtn).toBeInTheDocument();

    // The built-in card must NOT have a delete button for its own row.
    // The custom type has a delete button; the built-in does not.
    // Count: we expect exactly one delete button (for the custom type).
    // We get delete buttons by their icon class — use role="button" + aria or title.
    // Since Trash2Icon buttons have no title, we check that reset is present
    // and that only one delete-style button exists (the custom type's).
    const allButtons = screen.getAllByRole("button");
    const deleteButtons = allButtons.filter(
      (b) => b.classList.contains("text-destructive") ||
        b.closest("button")?.classList.contains("text-destructive"),
    );
    // Exactly one destructive button: the custom type's delete.
    expect(deleteButtons).toHaveLength(1);
  });

  it("custom types show a Delete button and no Reset button", async () => {
    renderSubagents();
    await waitFor(() =>
      expect(screen.getByText("My Agent")).toBeInTheDocument(),
    );
    // Only one Reset button exists (the built-in's).
    expect(screen.getAllByTitle("Reset to defaults")).toHaveLength(1);
  });

  it("clicking Reset calls resetSubagentType with the type id", async () => {
    renderSubagents();
    await waitFor(() =>
      expect(screen.getByText("Research Analyst")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTitle("Reset to defaults"));
    await waitFor(() =>
      expect(mockResetSubagentType).toHaveBeenCalledWith("deep-research"),
    );
  });

  it("clicking Delete calls deleteSubagentType with the type id", async () => {
    renderSubagents();
    await waitFor(() =>
      expect(screen.getByText("My Agent")).toBeInTheDocument(),
    );
    // The destructive button is the custom type's delete.
    const allButtons = screen.getAllByRole("button");
    const deleteBtn = allButtons.find((b) =>
      b.classList.contains("text-destructive") ||
      (b as HTMLElement).closest("button")?.classList.contains("text-destructive"),
    );
    expect(deleteBtn).toBeDefined();
    fireEvent.click(deleteBtn!);
    await waitFor(() =>
      expect(mockDeleteSubagentType).toHaveBeenCalledWith("my-agent"),
    );
  });

  it("clicking Edit opens the form dialog with the type's data", async () => {
    renderSubagents();
    await waitFor(() =>
      expect(screen.getByText("My Agent")).toBeInTheDocument(),
    );
    // Find all pencil/edit icon buttons (no title attribute = edit buttons)
    // and click the first one to open the dialog.
    const allButtons = screen.getAllByRole("button");
    // The first edit button (no title, no text-destructive) is for the first card.
    const firstEditBtn = allButtons.find(
      (b) =>
        !b.title &&
        !b.classList.contains("text-destructive") &&
        b.querySelector("svg"),
    );
    expect(firstEditBtn).toBeDefined();
    fireEvent.click(firstEditBtn!);
    // A dialog should open (the form fields should be visible).
    await waitFor(() =>
      expect(screen.getByPlaceholderText("my-agent-type")).toBeInTheDocument(),
    );
  });

  it("saving a new type calls saveSubagentType with the form data", async () => {
    renderSubagents();
    await waitFor(() =>
      expect(screen.getByText("Research Analyst")).toBeInTheDocument(),
    );

    // Open the new type dialog via the "New type" button.
    fireEvent.click(screen.getByText("New type"));
    await waitFor(() =>
      expect(screen.getByText("Create subagent type")).toBeInTheDocument(),
    );

    // Fill in the required fields.
    const idInput = screen.getByPlaceholderText("my-agent-type");
    fireEvent.change(idInput, { target: { value: "test-id" } });

    const nameInput = screen.getByPlaceholderText("My Agent Type");
    fireEvent.change(nameInput, { target: { value: "Test Agent" } });

    // Click Create.
    fireEvent.click(screen.getByText("Create"));
    await waitFor(() =>
      expect(mockSaveSubagentType).toHaveBeenCalledWith(
        expect.objectContaining({ id: "test-id", name: "Test Agent" }),
      ),
    );
  });

  it("shows execution_mode and tool_mode badges on cards", async () => {
    renderSubagents();
    await waitFor(() =>
      expect(screen.getByText("Research Analyst")).toBeInTheDocument(),
    );
    // The built-in type has execution_mode=background, tool_mode=include
    expect(screen.getByText("background")).toBeInTheDocument();
    expect(screen.getByText("include")).toBeInTheDocument();
    // The custom type has execution_mode=sync, tool_mode=all
    expect(screen.getByText("sync")).toBeInTheDocument();
    expect(screen.getByText("all")).toBeInTheDocument();
  });
});
