import { render, screen, waitFor } from "@testing-library/react";
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
  it("fetches and renders the report markdown", async () => {
    const { WorkspaceMarkdownViewer } = await import("./WorkspaceMarkdownViewer");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, text: async () => "# Hello report" })),
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
    await waitFor(() =>
      expect(screen.getByText(/Hello report/i)).toBeInTheDocument(),
    );
  });
});
