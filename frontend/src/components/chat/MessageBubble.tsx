import { useMemo, useState } from "react";
import hljs from "highlight.js/lib/core";
import DOMPurify from "dompurify";
import { MarkdownContent } from "@/components/ui/MarkdownContent";
import type { ChatMessageWithMeta, ToolUsageEntry } from "@/types/chat";
import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { ChevronRightIcon, WrenchIcon } from "lucide-react";

interface MessageBubbleProps {
  message: ChatMessageWithMeta;
  isShared: boolean;
  currentUserId?: string;
}

export function MessageBubble({
  message,
  isShared,
  currentUserId,
}: MessageBubbleProps) {
  const isOwnMessage = !isShared || !message.author_id || message.author_id === currentUserId;
  const isUser = message.role === "user" && isOwnMessage;
  const isAssistant = message.role === "assistant";

  let authorLabel = "";
  if (isAssistant) {
    authorLabel = "Gilbert";
  } else if (isShared && message.author_name) {
    authorLabel = isOwnMessage ? "You" : message.author_name;
  } else if (message.role === "user") {
    authorLabel = "You";
  }

  // Strip [Name]: prefix from shared room messages — stored for AI context
  // but shouldn't display since we show author_name separately
  let displayContent = message.content;
  if (isShared && message.role === "user") {
    displayContent = displayContent.replace(/^\[.*?\]:\s*/, "");
  }

  const initials = isAssistant
    ? "G"
    : (message.author_name || "You").charAt(0).toUpperCase();

  const toolUsage = isAssistant ? message.tool_usage : undefined;

  return (
    <div
      className={cn(
        "flex gap-2.5 max-w-3xl mx-auto",
        isUser ? "flex-row-reverse" : "flex-row",
      )}
    >
      <Avatar className="size-7 shrink-0 mt-0.5">
        <AvatarFallback
          className={cn(
            "text-[11px]",
            isAssistant && "bg-primary text-primary-foreground",
          )}
        >
          {initials}
        </AvatarFallback>
      </Avatar>

      <div
        className={cn(
          "flex flex-col gap-0.5 min-w-0",
          isUser ? "items-end" : "items-start",
        )}
      >
        <span className="text-[11px] text-muted-foreground px-0.5">
          {authorLabel}
        </span>
        <div
          className={cn(
            "rounded-2xl px-3.5 py-2 text-sm leading-relaxed",
            isUser
              ? "bg-primary text-primary-foreground rounded-tr-sm"
              : "bg-muted rounded-tl-sm",
          )}
        >
          {isAssistant ? (
            <MarkdownContent content={message.content} />
          ) : (
            <p className="whitespace-pre-wrap break-words">{displayContent}</p>
          )}
        </div>
        {toolUsage && toolUsage.length > 0 && (
          <ToolUsageFooter tools={toolUsage} />
        )}
      </div>
    </div>
  );
}

// ─── Tool usage display ───────────────────────────────────────────────

function ToolUsageFooter({ tools }: { tools: ToolUsageEntry[] }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="px-1 w-full">
      <button
        type="button"
        className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <WrenchIcon className="size-3" />
        <span>
          {tools.length} tool{tools.length !== 1 ? "s" : ""} used
        </span>
        <ChevronRightIcon
          className={cn(
            "size-3 transition-transform",
            expanded && "rotate-90",
          )}
        />
      </button>
      {expanded && (
        <div className="mt-1 space-y-1">
          {tools.map((t, idx) => (
            <ToolCallCard key={idx} tool={t} index={idx} />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolCallCard({
  tool,
  index,
}: {
  tool: ToolUsageEntry;
  index: number;
}) {
  const [open, setOpen] = useState(false);

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
        <span className="text-muted-foreground tabular-nums">#{index + 1}</span>
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
            <CollapsibleSection label="arguments" defaultOpen>
              <HighlightedContent
                value={tool.arguments}
                emptyLabel="(no arguments)"
              />
            </CollapsibleSection>
          )}
          {hasResult && (
            <CollapsibleSection label="result" defaultOpen>
              <HighlightedContent
                value={tool.result}
                emptyLabel="(no output)"
              />
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
}: {
  label: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-1 px-2 py-1 text-[10px] uppercase tracking-wide text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronRightIcon
          className={cn(
            "size-2.5 transition-transform",
            open && "rotate-90",
          )}
        />
        <span>{label}</span>
      </button>
      {open && <div className="px-2 pb-1.5">{children}</div>}
    </div>
  );
}

// ─── Content detection and highlighting ──────────────────────────────

type Detected = {
  html: string;
  language: string;
};

function detectAndHighlight(raw: unknown): Detected {
  // Objects / arrays / primitives that aren't strings → pretty-print as JSON.
  if (raw !== null && typeof raw === "object") {
    const pretty = safeStringify(raw);
    return highlightAs(pretty, "json");
  }
  if (typeof raw !== "string") {
    return { html: escapeHtml(String(raw)), language: "text" };
  }

  const text = raw;
  const trimmed = text.trim();
  if (!trimmed) {
    return { html: "", language: "text" };
  }

  // JSON detection: either object/array shape that parses cleanly.
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      const parsed = JSON.parse(trimmed);
      // Only reformat if the input wasn't already the pretty form — this
      // avoids ugly double-spacing on already-indented payloads.
      const pretty = JSON.stringify(parsed, null, 2);
      return highlightAs(pretty, "json");
    } catch {
      // fall through
    }
  }

  // XML / HTML-ish detection.
  if (trimmed.startsWith("<") && /<\/?\w[\w-]*/.test(trimmed)) {
    return highlightAs(text, "xml");
  }

  // Otherwise: plain text, escaped, no highlighting. Autodetect is tempting
  // but noisy for short/ambiguous strings.
  return { html: escapeHtml(text), language: "text" };
}

function highlightAs(code: string, language: string): Detected {
  try {
    const result = hljs.highlight(code, { language, ignoreIllegals: true });
    return {
      html: DOMPurify.sanitize(result.value),
      language,
    };
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
