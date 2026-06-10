import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { MarkdownContent } from "@/components/ui/MarkdownContent";

/**
 * Rewrite relative image/link targets in `md` to the authenticated workspace
 * download route, so embedded workspace media resolves. Absolute URLs (http(s)
 * or already-/api/...) are left untouched. `basePath` is the report's own
 * workspace path (e.g. "conv-1/outputs/report.md"); relatives resolve against
 * its directory.
 */
export function rewriteWorkspaceEmbeds(
  md: string,
  conversationId: string,
  basePath: string,
): string {
  const dir = basePath.includes("/") ? basePath.slice(0, basePath.lastIndexOf("/")) : "";
  // strip the leading "<conv>/" if present so dir is workspace-relative
  const relDir = dir.startsWith(conversationId + "/")
    ? dir.slice(conversationId.length + 1)
    : dir;
  const toUrl = (target: string): string => {
    if (
      /^(https?:)?\/\//i.test(target) ||
      target.startsWith("/") ||
      // already a download path (with or without leading slash)
      target.startsWith("api/chat/download/")
    )
      return target;
    const cleaned = target.replace(/^\.\//, "");
    const full =
      cleaned.startsWith("outputs/") ||
      cleaned.startsWith("scratch/") ||
      cleaned.startsWith("uploads/")
        ? cleaned
        : relDir
          ? `${relDir}/${cleaned}`
          : cleaned;
    return `/api/chat/download/${conversationId}/${full}`;
  };
  // ![alt](target) and [text](target)
  return md.replace(/(!?\[[^\]]*\])\(([^)\s]+)([^)]*)\)/g, (_m, label, target, rest) => {
    return `${label}(${toUrl(target)}${rest})`;
  });
}

export function WorkspaceMarkdownViewer({
  open,
  conversationId,
  path,
  onClose,
}: {
  open: boolean;
  conversationId: string;
  path: string;
  onClose: () => void;
}) {
  const [rawContent, setRawContent] = useState<string>("");
  const [content, setContent] = useState<string>("");
  const [error, setError] = useState<string>("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setRawContent("");
    setContent("");
    setError("");
    const url = `/api/chat/download/${encodeURIComponent(conversationId)}/${path
      .split("/")
      .map(encodeURIComponent)
      .join("/")}`;
    fetch(url, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((text) => {
        if (!cancelled) {
          setRawContent(text);
          setContent(
            rewriteWorkspaceEmbeds(text, conversationId, `${conversationId}/${path}`),
          );
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [open, conversationId, path]);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{path.split("/").pop()}</DialogTitle>
        </DialogHeader>
        {error ? (
          <p className="text-sm text-rose-400">Couldn't load report: {error}</p>
        ) : (
          <Tabs defaultValue="rendered">
            <TabsList variant="line">
              <TabsTrigger value="rendered">Rendered</TabsTrigger>
              <TabsTrigger value="raw">Raw</TabsTrigger>
            </TabsList>
            <TabsContent value="rendered">
              <MarkdownContent content={content} />
            </TabsContent>
            <TabsContent value="raw">
              <pre className="text-xs whitespace-pre-wrap break-words">
                <code>{rawContent}</code>
              </pre>
            </TabsContent>
          </Tabs>
        )}
      </DialogContent>
    </Dialog>
  );
}
