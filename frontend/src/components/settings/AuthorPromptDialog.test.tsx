import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthorPromptDialog } from "./AuthorPromptDialog";

vi.mock("@/hooks/useWebSocket", () => ({
  useWebSocket: () => ({ connected: true }),
}));

// Two RPCs list AI profiles in different shapes: ``listAiProfiles`` returns
// a bare array, ``listProfiles`` returns ``{profiles, all_tool_names, ...}``.
// They must not share a react-query cache key.
vi.mock("@/hooks/useWsApi", () => ({
  useWsApi: () => ({
    listAiProfiles: () =>
      Promise.resolve([{ name: "standard", description: "Standard tier" }]),
    listProfiles: () =>
      Promise.resolve({
        profiles: [{ name: "standard", description: "", tool_mode: "all", tools: [],
                     tool_roles: {}, backend: "", model: "", assigned_calls: [] }],
        declared_calls: [], profile_names: ["standard"], all_tool_names: [],
      }),
    listModels: () => Promise.resolve({ backends: [] }),
    authorPrompt: vi.fn(),
  }),
}));

describe("AuthorPromptDialog", () => {
  it("opens without crashing the profile editor link", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <AuthorPromptDialog
          open
          onClose={() => {}}
          namespace="inbox_rules"
          paramKey="match_condition"
          paramLabel="Condition"
          currentText="Old text."
          onApply={() => {}}
        />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Edit profile")).toBeInTheDocument());
  });
});
