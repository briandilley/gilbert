import { useMemo, useState, type ReactNode } from "react";
import hljs from "highlight.js/lib/core";
import DOMPurify from "dompurify";
import { MarkdownContent } from "@/components/ui/MarkdownContent";
import type {
  ChatRound,
  ChatRoundTool,
  ChatTurn,
  FileAttachment,
} from "@/types/chat";
import { isReferenceAttachment } from "@/types/chat";
import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { useWsApi } from "@/hooks/useWsApi";
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  DownloadIcon,
  FileIcon,
  FileTextIcon,
  LoaderIcon,
  SquareIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react";

interface TurnBubbleProps {
  turn: ChatTurn;
  isShared: boolean;
  currentUserId?: string;
}

export function TurnBubble({
  turn,
  isShared,
  currentUserId,
}: TurnBubbleProps) {
  const userAuthorId = turn.user_message.author_id || "";
  const userIsOwn =
    !isShared || !userAuthorId || userAuthorId === currentUserId;

  const userAuthorLabel =
    isShared && turn.user_message.author_name
      ? userIsOwn
        ? "You"
        : turn.user_message.author_name
      : "You";

  // Strip the "[Display Name]: " prefix from shared room user messages.
  // The prefix is stored for AI context but author_name is shown
  // separately so duplicating it here would be noise.
  let userContent = turn.user_message.content;
  if (isShared) {
    userContent = userContent.replace(/^\[.*?\]:\s*/, "");
  }

  const userInitials = (
    isShared && turn.user_message.author_name && !userIsOwn
      ? turn.user_message.author_name
      : "You"
  )
    .charAt(0)
    .toUpperCase();

  const hasFinal =
    turn.final_content.length > 0 || turn.final_attachments.length > 0;
  const hasRounds = turn.rounds.length > 0;

  return (
    <div className="space-y-2">
      {/* User message — right-aligned bubble, mirrors the old layout. */}
      <div className="flex flex-row-reverse gap-2.5 max-w-3xl mx-auto">
        <Avatar className="size-7 shrink-0 mt-0.5">
          <AvatarFallback className="text-[11px]">
            {userInitials}
          </AvatarFallback>
        </Avatar>
        <div className="flex flex-col gap-0.5 min-w-0 items-end">
          <span className="text-[11px] text-muted-foreground px-0.5">
            {userAuthorLabel}
          </span>
          {turn.user_message.attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 max-w-full justify-end">
              {turn.user_message.attachments.map((att, idx) => (
                <AttachmentChip key={idx} attachment={att} index={idx} />
              ))}
            </div>
          )}
          {userContent && (
            <div className="rounded-2xl px-3.5 py-2 text-sm leading-relaxed bg-primary text-primary-foreground rounded-tr-sm">
              <p className="whitespace-pre-wrap break-words">{userContent}</p>
            </div>
          )}
        </div>
      </div>

      {/* Assistant side — thinking card + final answer. */}
      {(hasRounds || hasFinal || turn.incomplete || turn.interrupted || turn.streaming) && (
        <div className="flex flex-row gap-2.5 max-w-3xl mx-auto">
          <Avatar className="size-7 shrink-0 mt-0.5">
            <AvatarFallback className="text-[11px] bg-primary text-primary-foreground">
              G
            </AvatarFallback>
          </Avatar>
          <div className="flex flex-col gap-1.5 min-w-0 items-start flex-1">
            <span className="text-[11px] text-muted-foreground px-0.5 flex items-center gap-1">
              Gilbert
              {turn.interrupted && (
                // Subtle stop indicator — a small filled square, the
                // universal "stopped" glyph. Tooltip spells it out for
                // a11y. Shown inline next to the "Gilbert" label so
                // it's visible whether or not the thinking card is
                // expanded and regardless of whether any final answer
                // was reached.
                <span
                  title="You interrupted this turn"
                  aria-label="Interrupted"
                  className="inline-flex"
                >
                  <SquareIcon className="size-2.5 fill-muted-foreground/70 text-muted-foreground/70" />
                </span>
              )}
            </span>

            {(hasRounds || (turn.streaming && !hasFinal)) && (
              <ThinkingCard turn={turn} />
            )}

            {hasFinal && (
              <FinalAnswer turn={turn} />
            )}

            {!hasFinal && turn.incomplete && !turn.interrupted && (
              <div className="flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-700 dark:text-amber-300">
                <AlertTriangleIcon className="size-3.5 shrink-0" />
                <span>
                  Gilbert didn't reach a final answer (loop limit or error).
                  Try retrying the message.
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Thinking card ────────────────────────────────────────────────────

function ThinkingCard({ turn }: { turn: ChatTurn }) {
  // Always start collapsed — the user gets a live one-line preview of
  // the most recent reasoning + tool in the header. Click to expand
  // for the full per-round breakdown.
  const [expanded, setExpanded] = useState(false);

  const totalTools = turn.rounds.reduce((n, r) => n + r.tools.length, 0);
  const lastRoundIdx = turn.rounds.length - 1;
  // The "current" round — the one that pulses while streaming — is
  // the most recent round when there's no final answer yet. Once the
  // final answer arrives, no round pulses (the work is done).
  const currentRoundIdx =
    turn.streaming && !turn.final_content && lastRoundIdx >= 0
      ? lastRoundIdx
      : -1;

  // Build the collapsed header preview: the most recent activity. We
  // prefer the most recent tool that's been started (running or done)
  // when there is one, falling back to the most recent reasoning text
  // for text-only rounds. The preview updates live as new events
  // arrive, since it's just derived state.
  const lastRound = lastRoundIdx >= 0 ? turn.rounds[lastRoundIdx] : null;
  const lastTool =
    lastRound && lastRound.tools.length > 0
      ? lastRound.tools[lastRound.tools.length - 1]
      : null;
  const lastReasoningSnippet = (() => {
    // Walk rounds backwards looking for the most recent non-empty
    // reasoning text. Truncate to ~80 chars on the most recent line
    // so the header stays single-line.
    for (let i = turn.rounds.length - 1; i >= 0; i--) {
      const r = turn.rounds[i].reasoning.trim();
      if (r) {
        const lastLine = r.split(/\n+/).pop() ?? "";
        return lastLine.length > 80 ? lastLine.slice(0, 80) + "…" : lastLine;
      }
    }
    return "";
  })();

  const isLive = turn.streaming === true && !turn.final_content;
  const totalRounds = turn.rounds.length;

  return (
    <div
      className={cn(
        "w-full max-w-2xl rounded-lg border bg-muted/30",
        isLive && "border-dashed border-muted-foreground/40",
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "w-full flex items-start gap-1.5 px-3 py-1.5 text-left hover:bg-muted/40 transition-colors",
          isLive && "animate-pulse",
        )}
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 mt-1 text-muted-foreground transition-transform",
            expanded && "rotate-90",
          )}
        />
        <div className="min-w-0 flex-1 space-y-0.5">
          {/* Top line: status icon + most recent activity label */}
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            {isLive ? (
              <LoaderIcon className="size-3 animate-spin shrink-0" />
            ) : (
              <WrenchIcon className="size-3 shrink-0" />
            )}
            {lastTool ? (
              <>
                <span className="font-mono font-medium text-foreground/85 truncate">
                  {lastTool.tool_name}
                </span>
                {lastTool.is_error && (
                  <span className="text-destructive font-medium uppercase tracking-wide text-[10px]">
                    error
                  </span>
                )}
                <span className="text-[10px] tabular-nums opacity-70 ml-auto shrink-0">
                  {totalRounds} round{totalRounds === 1 ? "" : "s"} ·{" "}
                  {totalTools} tool{totalTools === 1 ? "" : "s"}
                </span>
              </>
            ) : isLive ? (
              <span className="italic">Thinking…</span>
            ) : (
              <span>
                {totalRounds} round{totalRounds === 1 ? "" : "s"}, {totalTools}{" "}
                tool{totalTools === 1 ? "" : "s"}
              </span>
            )}
          </div>
          {/* Second line: a snippet of the most recent reasoning text */}
          {lastReasoningSnippet && !expanded && (
            <div className="text-[11px] text-muted-foreground/80 italic leading-snug truncate">
              {lastReasoningSnippet}
            </div>
          )}
        </div>
      </button>
      {expanded && (
        <div className="border-t divide-y divide-border/50">
          {turn.rounds.map((round, i) => (
            <RoundView
              key={i}
              round={round}
              roundNumber={i + 1}
              isCurrent={i === currentRoundIdx}
            />
          ))}
          {/* If the turn is streaming and there are no rounds yet, show
              a placeholder so the bubble has body. */}
          {turn.streaming && turn.rounds.length === 0 && (
            <div className="px-3 py-2 text-[11px] text-muted-foreground italic animate-pulse">
              Gilbert is starting…
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RoundView({
  round,
  roundNumber,
  isCurrent,
}: {
  round: ChatRound;
  roundNumber: number;
  isCurrent: boolean;
}) {
  return (
    <div
      className={cn(
        "px-3 py-2 space-y-1.5",
        isCurrent && "animate-pulse",
      )}
    >
      <div className="flex items-baseline gap-2 text-[10px] uppercase tracking-wide text-muted-foreground">
        <span className="tabular-nums">round {roundNumber}</span>
      </div>
      {round.reasoning && (
        <div
          className={cn(
            "text-[12px] leading-snug whitespace-pre-wrap text-foreground/85",
            isCurrent && "italic",
          )}
        >
          {round.reasoning}
          {isCurrent && (
            <span
              aria-hidden="true"
              className="ml-0.5 inline-block h-3 w-[1.5px] -mb-0.5 align-baseline bg-muted-foreground/70 animate-caret-blink"
            />
          )}
        </div>
      )}
      {round.tools.length > 0 && (
        <div className="space-y-1 mt-1">
          {round.tools.map((tool, i) => (
            <ToolEntry key={tool.tool_call_id || i} tool={tool} />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolEntry({ tool }: { tool: ChatRoundTool }) {
  const [open, setOpen] = useState(false);
  const status = tool.status ?? "done";
  const hasArgs =
    tool.arguments !== undefined &&
    tool.arguments !== null &&
    Object.keys(tool.arguments).length > 0;
  const hasResult = tool.result !== undefined && tool.result !== "";

  return (
    <div
      className={cn(
        "rounded-md border bg-background/60 text-[11px] overflow-hidden",
        tool.is_error && "border-destructive/40",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className={cn(
          "w-full flex items-center gap-1.5 px-2 py-1 text-left hover:bg-muted/60 transition-colors",
          open && "border-b",
          tool.is_error && "bg-destructive/10",
        )}
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        {status === "running" ? (
          <LoaderIcon className="size-3 shrink-0 animate-spin text-blue-500" />
        ) : tool.is_error ? (
          <XIcon className="size-3 shrink-0 text-destructive" />
        ) : (
          <CheckIcon className="size-3 shrink-0 text-green-500" />
        )}
        <span className="font-mono font-medium text-foreground truncate">
          {tool.tool_name || "(unknown)"}
        </span>
        {tool.is_error && (
          <span className="ml-auto text-destructive font-medium uppercase tracking-wide text-[10px]">
            error
          </span>
        )}
      </button>
      {open && (
        <div className="divide-y">
          {hasArgs && (
            <CollapsibleSection
              label="arguments"
              defaultOpen
              copyText={() => toCopyText(tool.arguments)}
              copyLabel="Copy arguments"
            >
              <HighlightedContent
                value={tool.arguments}
                emptyLabel="(no arguments)"
              />
            </CollapsibleSection>
          )}
          {hasResult && (
            <CollapsibleSection
              label="result"
              defaultOpen
              copyText={() => toCopyText(tool.result)}
              copyLabel="Copy result"
            >
              <HighlightedContent value={tool.result} emptyLabel="(no output)" />
            </CollapsibleSection>
          )}
          {!hasArgs && !hasResult && (
            <div className="px-2 py-1.5 text-[11px] text-muted-foreground italic">
              No arguments or result.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CollapsibleSection({
  label,
  defaultOpen = false,
  children,
  copyText,
  copyLabel,
}: {
  label: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
  copyText?: () => string;
  copyLabel?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  // Split header into a toggle button and (optionally) a copy button
  // side-by-side. A nested ``<button>`` inside the toggle ``<button>``
  // would be invalid HTML, so they're siblings in a flex row.
  return (
    <div>
      <div className="w-full flex items-center gap-1 px-2 py-1 text-[10px] uppercase tracking-wide text-muted-foreground">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex items-center gap-1 flex-1 min-w-0 hover:text-foreground transition-colors text-left"
        >
          <ChevronRightIcon
            className={cn("size-2.5 transition-transform", open && "rotate-90")}
          />
          <span>{label}</span>
        </button>
        {copyText && (
          <CopyButton
            getText={copyText}
            label={copyLabel ?? `Copy ${label}`}
            className="shrink-0"
          />
        )}
      </div>
      {open && <div className="px-2 pb-1.5">{children}</div>}
    </div>
  );
}

// ─── Final answer ─────────────────────────────────────────────────────

function FinalAnswer({ turn }: { turn: ChatTurn }) {
  return (
    <div className="space-y-1.5 max-w-2xl">
      {turn.final_attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {turn.final_attachments.map((att, idx) => (
            <AttachmentChip key={idx} attachment={att} index={idx} />
          ))}
        </div>
      )}
      {turn.final_content && (
        // ``group relative`` + absolutely-positioned copy button: the
        // button only fades in on hover so the bubble stays clean at
        // rest. We copy the raw Markdown (``turn.final_content``)
        // rather than the rendered DOM text — that way pasting back
        // into any Markdown surface round-trips losslessly.
        <div className="group relative rounded-2xl px-3.5 py-2 text-sm leading-relaxed bg-muted rounded-tl-sm">
          <MarkdownContent content={turn.final_content} />
          <div className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
            <CopyButton
              getText={() => turn.final_content}
              label="Copy response"
              stopPropagation={false}
              className="bg-background/70 backdrop-blur-sm"
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Attachment chip (shared between user and assistant) ─────────────

function AttachmentChip({
  attachment,
  index,
}: {
  attachment: FileAttachment;
  index: number;
}) {
  const api = useWsApi();
  const [busy, setBusy] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const isReference = isReferenceAttachment(attachment);

  async function handleReferenceDownload(): Promise<void> {
    if (!isReference || busy) return;
    setBusy(true);
    setDownloadError(null);
    try {
      const resp = await api.downloadSkillWorkspaceFile(
        attachment.workspace_skill ?? "",
        attachment.workspace_path ?? "",
        attachment.workspace_conv || undefined,
      );
      const mediaType =
        resp.media_type || attachment.media_type || "application/octet-stream";
      const buffer = base64ToArrayBuffer(resp.content_base64);
      const blob = new Blob([buffer], { type: mediaType });
      const url = URL.createObjectURL(blob);
      try {
        const a = document.createElement("a");
        a.href = url;
        a.download = attachment.name || resp.filename || "download";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 0);
      }
    } catch (err) {
      console.error("Workspace file download failed:", err);
      // Show the error inline on the chip so the user actually sees
      // it instead of a silent no-op. 404 is the most common case —
      // the conversation that produced the file was deleted (which
      // also wipes the per-conversation workspace), or the file got
      // moved/removed since the chip was rendered.
      const message =
        err instanceof Error && err.message
          ? err.message
          : typeof err === "string"
            ? err
            : "Download failed";
      const friendly = /not found|404/i.test(message)
        ? "File no longer available — the chat that produced it was likely deleted."
        : message;
      setDownloadError(friendly);
    } finally {
      setBusy(false);
    }
  }

  // Reusable chip shell for any reference-mode attachment.
  const refChip = (label: string, sublabel: string, icon: ReactNode) => (
    <div className="flex flex-col gap-1 max-w-xs">
      <button
        type="button"
        onClick={handleReferenceDownload}
        disabled={busy}
        className={cn(
          "flex items-center gap-2 rounded-lg border bg-muted/50 px-3 py-2 text-left hover:bg-muted disabled:opacity-60",
          downloadError && "border-destructive/50 bg-destructive/5",
        )}
        title={`Download ${attachment.name ?? "file"}`}
      >
        <div className="flex size-9 shrink-0 items-center justify-center rounded bg-background">
          {icon}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium">{label}</div>
          <div className="truncate text-[10px] text-muted-foreground">
            {sublabel}
          </div>
        </div>
        {busy ? (
          <LoaderIcon className="size-4 animate-spin text-muted-foreground shrink-0" />
        ) : (
          <DownloadIcon className="size-4 text-muted-foreground shrink-0" />
        )}
      </button>
      {downloadError && (
        <div className="flex items-start gap-1 text-[10px] text-destructive leading-snug px-0.5">
          <AlertTriangleIcon className="size-3 shrink-0 mt-px" />
          <span>{downloadError}</span>
        </div>
      )}
    </div>
  );

  if (attachment.kind === "image") {
    if (isReference) {
      return refChip(
        attachment.name || `image ${index + 1}`,
        `${attachment.media_type} · workspace file`,
        <FileIcon className="size-5 text-muted-foreground" />,
      );
    }
    const src = `data:${attachment.media_type};base64,${attachment.data ?? ""}`;
    return (
      <a
        href={src}
        target="_blank"
        rel="noreferrer"
        className="block overflow-hidden rounded-lg border bg-muted"
      >
        <img
          src={src}
          alt={attachment.name || `attachment ${index + 1}`}
          className="max-h-60 max-w-[16rem] object-cover"
        />
      </a>
    );
  }

  if (attachment.kind === "document" || attachment.kind === "file") {
    // ``document`` is the AI-readable path (PDF, xlsx converted to
    // text server-side). ``file`` is the opaque catch-all for every
    // other upload the user made. Both render as the same download
    // chip shape but the bytes come from one of three sources:
    //
    // 1. ``chat-uploads`` workspace reference: user uploaded this
    //    via ``POST /api/chat/upload``. Download via the streaming
    //    HTTP endpoint ``GET /api/chat/download/{conv}/{path}`` so
    //    the browser handles 1 GB downloads natively instead of
    //    choking on a base64 blob over the WebSocket.
    // 2. Any other reference (tool-produced PDFs, generated
    //    images, etc.): fall back to the WS-based
    //    ``skills.workspace.download`` RPC via ``refChip``.
    // 3. Inline ``data``: render a ``data:`` URL directly.
    if (isReference) {
      if (attachment.workspace_skill === "chat-uploads") {
        const convId = attachment.workspace_conv || "";
        const path = attachment.workspace_path || "";
        // The browser handles the streaming download natively,
        // including progress and save-dialog. No need for the
        // WS-based base64 download.
        const href = `/api/chat/download/${encodeURIComponent(convId)}/${encodeURIComponent(path)}`;
        const sizeLabel =
          attachment.size && attachment.size > 0
            ? ` · ${formatAttachmentBytes(attachment.size)}`
            : "";
        return (
          <a
            href={href}
            download={attachment.name || "download"}
            className="flex max-w-xs items-center gap-2 rounded-lg border bg-muted/50 px-3 py-2 no-underline hover:bg-muted"
            title={`Download ${attachment.name ?? "file"}`}
          >
            <div className="flex size-9 shrink-0 items-center justify-center rounded bg-background">
              <FileIcon className="size-5 text-muted-foreground" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-medium">
                {attachment.name}
              </div>
              <div className="truncate text-[10px] text-muted-foreground">
                {mediaTypeLabel(attachment.media_type)}
                {sizeLabel}
              </div>
            </div>
            <DownloadIcon className="size-4 text-muted-foreground shrink-0" />
          </a>
        );
      }
      return refChip(
        attachment.name || "document",
        `${mediaTypeLabel(attachment.media_type)} · workspace file`,
        <FileIcon className="size-5 text-muted-foreground" />,
      );
    }
    const inlineData = attachment.data ?? "";
    const bytes = Math.floor((inlineData.length * 3) / 4);
    const effectiveType = attachment.media_type || "application/octet-stream";
    const href = `data:${effectiveType};base64,${inlineData}`;
    return (
      <a
        href={href}
        download={attachment.name || "download"}
        target="_blank"
        rel="noreferrer"
        className="flex max-w-xs items-center gap-2 rounded-lg border bg-muted/50 px-3 py-2 no-underline hover:bg-muted"
      >
        <div className="flex size-9 shrink-0 items-center justify-center rounded bg-background">
          <FileIcon className="size-5 text-muted-foreground" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium">{attachment.name}</div>
          <div className="truncate text-[10px] text-muted-foreground">
            {mediaTypeLabel(effectiveType)} ·{" "}
            {formatAttachmentBytes(bytes)}
          </div>
        </div>
      </a>
    );
  }

  // Text attachment
  if (isReference) {
    return refChip(
      attachment.name || "file",
      `${mediaTypeLabel(attachment.media_type)} · workspace file`,
      <FileTextIcon className="size-5 text-muted-foreground" />,
    );
  }
  const inlineText = attachment.text ?? "";
  const bytes = new Blob([inlineText]).size;
  return (
    <div className="flex max-w-xs items-center gap-2 rounded-lg border bg-muted/50 px-3 py-2">
      <div className="flex size-9 shrink-0 items-center justify-center rounded bg-background">
        <FileTextIcon className="size-5 text-muted-foreground" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-medium">{attachment.name}</div>
        <div className="truncate text-[10px] text-muted-foreground">
          Text · {formatAttachmentBytes(bytes)}
        </div>
      </div>
    </div>
  );
}

function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64);
  const buffer = new ArrayBuffer(binary.length);
  const view = new Uint8Array(buffer);
  for (let i = 0; i < binary.length; i++) {
    view[i] = binary.charCodeAt(i);
  }
  return buffer;
}

function formatAttachmentBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function mediaTypeLabel(mt: string): string {
  if (!mt) return "File";
  if (mt === "application/pdf") return "PDF";
  if (mt.startsWith("image/")) return mt.slice(6).toUpperCase();
  if (mt.startsWith("text/")) return "Text";
  if (mt === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") {
    return "Excel";
  }
  return mt.split("/").pop()?.toUpperCase() ?? "File";
}

// ─── Highlighted content (for tool args/results) ─────────────────────

type Detected = { html: string; language: string };

function detectAndHighlight(raw: unknown): Detected {
  if (raw !== null && typeof raw === "object") {
    const pretty = safeStringify(raw);
    return highlightAs(pretty, "json");
  }
  if (typeof raw !== "string") {
    return { html: escapeHtml(String(raw)), language: "text" };
  }
  const text = raw;
  const trimmed = text.trim();
  if (!trimmed) return { html: "", language: "text" };

  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      const parsed = JSON.parse(trimmed);
      const pretty = JSON.stringify(parsed, null, 2);
      return highlightAs(pretty, "json");
    } catch {
      // fall through
    }
  }
  if (trimmed.startsWith("<") && /<\/?\w[\w-]*/.test(trimmed)) {
    return highlightAs(text, "xml");
  }
  return { html: escapeHtml(text), language: "text" };
}

function highlightAs(code: string, language: string): Detected {
  try {
    const result = hljs.highlight(code, { language, ignoreIllegals: true });
    return { html: DOMPurify.sanitize(result.value), language };
  } catch {
    return { html: escapeHtml(code), language: "text" };
  }
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ─── Copy button ─────────────────────────────────────────────────────

/**
 * Small icon-only copy button. Uses a getter so the text can be lazily
 * built from current state on click, rather than eagerly on every
 * render. Briefly swaps to a check icon for ~1.5s after a successful
 * copy as visual feedback.
 *
 * ``navigator.clipboard.writeText`` is unavailable in insecure contexts
 * (http:// on non-localhost) — we silently swallow the rejection
 * rather than crashing, matching the pattern in McpPromptPanel.
 */
function CopyButton({
  getText,
  label = "Copy",
  className,
  stopPropagation = true,
}: {
  getText: () => string;
  label?: string;
  className?: string;
  stopPropagation?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    if (stopPropagation) e.stopPropagation();
    try {
      await navigator.clipboard.writeText(getText());
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Insecure context, clipboard permission denied, etc. No-op.
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      className={cn(
        "inline-flex items-center justify-center rounded p-0.5 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors",
        className,
      )}
      title={copied ? "Copied" : label}
      aria-label={copied ? "Copied to clipboard" : label}
    >
      {copied ? (
        <CheckIcon className="size-3" />
      ) : (
        <CopyIcon className="size-3" />
      )}
    </button>
  );
}

/**
 * Normalize a tool argument / result value into a clipboard-friendly
 * string. Objects become pretty-printed JSON. Strings that *look* like
 * JSON get re-pretty-printed so a payload that happened to arrive as a
 * minified blob round-trips out as readable text. Everything else
 * falls through to ``String(value)``.
 */
function toCopyText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    ) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return value;
      }
    }
    return value;
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function HighlightedContent({
  value,
  emptyLabel,
}: {
  value: unknown;
  emptyLabel: string;
}) {
  const detected = useMemo(() => detectAndHighlight(value), [value]);
  if (!detected.html) {
    return (
      <div className="text-[11px] text-muted-foreground italic">
        {emptyLabel}
      </div>
    );
  }
  return (
    <pre
      className={cn(
        "hljs font-mono whitespace-pre-wrap break-all text-foreground/90 leading-snug text-[11px] rounded-sm px-1.5 py-1 overflow-x-auto max-h-80",
        detected.language !== "text" && `language-${detected.language}`,
      )}
      dangerouslySetInnerHTML={{ __html: detected.html }}
    />
  );
}
