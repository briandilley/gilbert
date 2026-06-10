import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AuthProvider } from "@/hooks/useAuth";
import { rewriteWorkspaceEmbeds } from "./WorkspaceMarkdownViewer";

describe("rewriteWorkspaceEmbeds", () => {
  const base = "conv-1/outputs/report.md";
  it("rewrites a relative image to the download route", () => {
    const out = rewriteWorkspaceEmbeds("![c](chart.png)", "conv-1", base);
    expect(out).toContain("/api/chat/download/conv-1/outputs/chart.png");
  });
  it("rewrites a relative outputs/ path", () => {
    const out = rewriteWorkspaceEmbeds("![c](outputs/a.png)", "conv-1", base);
    expect(out).toContain("/api/chat/download/conv-1/outputs/a.png");
  });
  it("leaves absolute and http urls untouched", () => {
    const md = "![x](https://e.com/i.png) and ![y](/api/chat/download/conv-1/outputs/z.png)";
    const out = rewriteWorkspaceEmbeds(md, "conv-1", base);
    expect(out).toContain("https://e.com/i.png");
    expect(out).toContain("/api/chat/download/conv-1/outputs/z.png");
    expect(out).not.toContain("/api/chat/download/conv-1/outputs/https");
  });
});

describe("WorkspaceMarkdownViewer", () => {
  it("renders markdown (not raw source) by default", async () => {
    const { WorkspaceMarkdownViewer } = await import("./WorkspaceMarkdownViewer");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, text: async () => "# Hello report\n\nbody text" })),
    );
    const { container } = render(
      <AuthProvider>
        <WorkspaceMarkdownViewer
          open
          conversationId="conv-1"
          path="outputs/report.md"
          onClose={() => {}}
        />
      </AuthProvider>,
    );
    // The Rendered tab must be active by default: a real <h1> heading element,
    // and NO raw <pre> source block visible.
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: /Hello report/i }),
      ).toBeInTheDocument(),
    );
    expect(container.querySelector("pre")).toBeNull();
  });

  it("shows raw markdown in a pre when Raw tab is clicked", async () => {
    const { WorkspaceMarkdownViewer } = await import("./WorkspaceMarkdownViewer");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, text: async () => "# Hello" })),
    );
    render(
      <AuthProvider>
        <WorkspaceMarkdownViewer
          open
          conversationId="conv-1"
          path="outputs/report.md"
          onClose={() => {}}
        />
      </AuthProvider>,
    );
    // Wait for fetch to complete and Rendered tab to show content
    await waitFor(() => screen.getByText(/Raw/i));
    // Click Raw tab and verify the raw markdown text appears in a pre
    fireEvent.click(screen.getByText(/Raw/i));
    await waitFor(() =>
      expect(screen.getByText(/^# Hello/)).toBeInTheDocument(),
    );
    // Switch back — rendered tab content should be visible again
    fireEvent.click(screen.getByText(/Rendered/i));
    // The # Hello raw text should no longer be visible (panel hidden)
    await waitFor(() =>
      expect(screen.queryByText(/^# Hello/)).not.toBeInTheDocument(),
    );
  });
});
