/**
 * Settings page for MCP servers running on the user's own machine,
 * bridged through this browser tab. Entries live in localStorage and
 * are announced to Gilbert on save and on every WS reconnect.
 */

import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  PlusIcon,
  RefreshCcwIcon,
  Trash2Icon,
  CheckCircle2Icon,
  AlertCircleIcon,
  InfoIcon,
} from "lucide-react";
import {
  useLocalMcpServers,
  type BridgeAnnounceResult,
  type LocalMcpServer,
} from "@/hooks/useMcpBridge";
import { useWebSocket } from "@/hooks/useWebSocket";
import { PageHeader } from "@/components/layout/PageHeader";

const SLUG_RE = /^[a-z][a-z0-9-]*$/;

interface DraftRow extends LocalMcpServer {
  // transient key for row identity while editing (slug can change);
  // not persisted. Stable across edits of a single session.
  rowKey: string;
}

function makeRowKey(): string {
  return `row_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}

function validateDraft(draft: DraftRow, others: DraftRow[]): string | null {
  if (!draft.slug.trim()) return "slug is required";
  if (!SLUG_RE.test(draft.slug))
    return "slug must be lowercase letters/digits/hyphens, starting with a letter";
  if (draft.slug.includes("__")) return "slug must not contain '__'";
  if (!draft.name.trim()) return "name is required";
  if (!draft.url.trim()) return "url is required";
  try {
    const parsed = new URL(draft.url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return "url must start with http:// or https://";
    }
  } catch {
    return "url is not a valid URL";
  }
  if (others.some((o) => o.rowKey !== draft.rowKey && o.slug === draft.slug)) {
    return "another row already uses this slug";
  }
  return null;
}

export function McpLocalPage() {
  const { servers, setServers, announce } = useLocalMcpServers();
  const { connected } = useWebSocket();
  const [drafts, setDrafts] = useState<DraftRow[]>([]);
  const [lastResults, setLastResults] = useState<
    Map<string, BridgeAnnounceResult>
  >(new Map());
  const [announcing, setAnnouncing] = useState(false);

  // Seed drafts from persisted state whenever the underlying list
  // changes (on mount, cross-tab edits, etc.).
  useEffect(() => {
    setDrafts((prev) => {
      const prevByKey = new Map(prev.map((d) => [`${d.slug}|${d.url}`, d]));
      return servers.map((s) => {
        const key = `${s.slug}|${s.url}`;
        const existing = prevByKey.get(key);
        return existing ?? { ...s, rowKey: makeRowKey() };
      });
    });
  }, [servers]);

  const resultsBySlug = useMemo(() => lastResults, [lastResults]);

  const addRow = () => {
    setDrafts((prev) => [
      ...prev,
      { slug: "", name: "", url: "http://localhost:8931/mcp", rowKey: makeRowKey() },
    ]);
  };

  const updateRow = (rowKey: string, patch: Partial<LocalMcpServer>) => {
    setDrafts((prev) =>
      prev.map((d) => (d.rowKey === rowKey ? { ...d, ...patch } : d)),
    );
  };

  const removeRow = (rowKey: string) => {
    setDrafts((prev) => prev.filter((d) => d.rowKey !== rowKey));
  };

  const validationErrors = useMemo(() => {
    const errs = new Map<string, string>();
    for (const d of drafts) {
      const err = validateDraft(d, drafts);
      if (err) errs.set(d.rowKey, err);
    }
    return errs;
  }, [drafts]);

  const anyErrors = validationErrors.size > 0;

  const save = async () => {
    if (anyErrors) return;
    const next = drafts.map(({ rowKey: _rk, ...rest }) => rest);
    setServers(next);
    if (connected && next.length > 0) {
      setAnnouncing(true);
      try {
        const results = await announce();
        const bySlug = new Map<string, BridgeAnnounceResult>();
        for (const r of results) {
          if (r.slug) bySlug.set(r.slug, r);
        }
        setLastResults(bySlug);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        const bySlug = new Map<string, BridgeAnnounceResult>();
        for (const s of next) {
          bySlug.set(s.slug, { slug: s.slug, ok: false, error: message });
        }
        setLastResults(bySlug);
      } finally {
        setAnnouncing(false);
      }
    } else if (next.length === 0) {
      setLastResults(new Map());
    }
  };

  const reAnnounce = async () => {
    if (!connected) return;
    setAnnouncing(true);
    try {
      const results = await announce();
      const bySlug = new Map<string, BridgeAnnounceResult>();
      for (const r of results) {
        if (r.slug) bySlug.set(r.slug, r);
      }
      setLastResults(bySlug);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const bySlug = new Map<string, BridgeAnnounceResult>();
      for (const s of servers) {
        bySlug.set(s.slug, { slug: s.slug, ok: false, error: message });
      }
      setLastResults(bySlug);
    } finally {
      setAnnouncing(false);
    }
  };

  const dirty = useMemo(() => {
    if (drafts.length !== servers.length) return true;
    for (let i = 0; i < drafts.length; i++) {
      const d = drafts[i];
      const s = servers[i];
      if (!s || d.slug !== s.slug || d.name !== s.name || d.url !== s.url) {
        return true;
      }
    }
    return false;
  }, [drafts, servers]);

  return (
    <div>
      <PageHeader
        eyebrow="MCP"
        title="Local servers"
        description="MCP servers running on your machine, bridged through this browser tab. Tools are only available to you, only while this tab is open. The URL you enter never leaves your browser — Gilbert forwards MCP requests over the WebSocket and your browser proxies them to the local URL."
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={reAnnounce}
              disabled={!connected || announcing || servers.length === 0}
            >
              <RefreshCcwIcon />
              Re-announce
            </Button>
            <Button size="sm" onClick={addRow}>
              <PlusIcon />
              Add server
            </Button>
          </>
        }
      />

      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        {/* CORS instructions inset — uses ``info`` tone via a hairline
            + signal-color icon, not a colored fill. */}
        <div className="mb-4 flex gap-3 rounded-md border border-border bg-subtle/40 p-3 text-sm">
          <InfoIcon className="size-4 shrink-0 mt-0.5 text-info" />
          <div className="space-y-1 min-w-0">
            <p>
              Your local MCP server must respond with CORS headers so
              this page can reach it. For most SDKs that means:
            </p>
            <pre className="text-xs bg-foreground/[0.04] rounded-sm border border-border px-2 py-1 overflow-x-auto font-mono">
{`Access-Control-Allow-Origin: ${window.location.origin}
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: content-type
Access-Control-Allow-Private-Network: true`}
            </pre>
            <p className="text-xs text-muted-foreground">
              The last header is only required in Chromium browsers when
              reaching a private-network address (including{" "}
              <code>localhost</code>) from an HTTPS Gilbert.
            </p>
          </div>
        </div>

      {drafts.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border py-16 text-center">
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
            No local MCP servers
          </p>
          <p className="max-w-md text-sm text-muted-foreground">
            Add a server to bridge a local MCP service through this tab.
          </p>
          <Button size="sm" onClick={addRow}>
            <PlusIcon />
            Add server
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          {drafts.map((draft) => {
            const err = validationErrors.get(draft.rowKey);
            const result = resultsBySlug.get(draft.slug);
            return (
              <Card key={draft.rowKey}>
                <CardContent className="pt-4 pb-4 space-y-3">
                  <div className="grid grid-cols-1 sm:grid-cols-[140px_1fr_auto] gap-3">
                    <div className="space-y-1">
                      <Label className="text-xs">Slug</Label>
                      <Input
                        value={draft.slug}
                        placeholder="fs"
                        onChange={(e) =>
                          updateRow(draft.rowKey, {
                            slug: e.target.value.toLowerCase(),
                          })
                        }
                        className="font-mono"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs">Display name</Label>
                      <Input
                        value={draft.name}
                        placeholder="Filesystem"
                        onChange={(e) =>
                          updateRow(draft.rowKey, { name: e.target.value })
                        }
                      />
                    </div>
                    <div className="flex items-end">
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={() => removeRow(draft.rowKey)}
                        aria-label="Remove row"
                      >
                        <Trash2Icon className="size-4 text-destructive" />
                      </Button>
                    </div>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs">URL</Label>
                    <Input
                      value={draft.url}
                      placeholder="http://localhost:8931/mcp"
                      onChange={(e) =>
                        updateRow(draft.rowKey, { url: e.target.value })
                      }
                      className="font-mono"
                    />
                  </div>
                  {err ? (
                    <div className="flex items-center gap-2 text-xs text-destructive">
                      <AlertCircleIcon className="size-3" />
                      {err}
                    </div>
                  ) : null}
                  {result ? (
                    result.ok ? (
                      <div className="flex items-center gap-2 text-xs text-emerald-600 dark:text-emerald-400">
                        <CheckCircle2Icon className="size-3" />
                        Announced ·{" "}
                        <Badge variant="secondary" className="text-xs">
                          {result.tool_count ?? 0} tools
                        </Badge>
                      </div>
                    ) : (
                      <div className="flex items-start gap-2 text-xs text-destructive">
                        <AlertCircleIcon className="size-3 mt-0.5 shrink-0" />
                        <span className="break-all">{result.error}</span>
                      </div>
                    )
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <div className="sticky bottom-4 mt-6 flex justify-end">
        <div className="flex items-center gap-2 bg-background/95 backdrop-blur border rounded-lg px-3 py-2 shadow-md">
          {anyErrors ? (
            <span className="text-xs text-destructive">
              Fix validation errors to save
            </span>
          ) : dirty ? (
            <span className="text-xs text-muted-foreground">
              Unsaved changes
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">Saved</span>
          )}
          <Button
            size="sm"
            onClick={save}
            disabled={anyErrors || !dirty || announcing}
          >
            {announcing ? "Announcing…" : "Save & announce"}
          </Button>
        </div>
      </div>
      </div>
    </div>
  );
}
