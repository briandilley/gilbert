import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  FileIcon,
  FileTextIcon,
  PaperclipIcon,
  SendHorizontalIcon,
  XIcon,
} from "lucide-react";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { SlashCommand, SlashParameter } from "@/types/slash";
import type { FileAttachment } from "@/types/chat";

/** A pending attachment owned by the chat page. Always carries an ``id``
 *  and user-visible ``name``; ``preview`` is only set for images so the
 *  thumbnail strip can render a blob URL. */
export interface PendingAttachment {
  id: string;
  name: string;
  attachment: FileAttachment;
  preview?: string;
}

export const MAX_CHAT_ATTACHMENTS = 8;
const MAX_IMAGE_DIMENSION = 1568;
const JPEG_QUALITY = 0.85;
const MAX_DOCUMENT_BYTES = 32 * 1024 * 1024;
const MAX_TEXT_BYTES = 512 * 1024;
const ALLOWED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

const TEXT_MIME_ALLOWLIST = new Set([
  "application/json",
  "application/xml",
  "application/javascript",
  "application/typescript",
  "application/x-sh",
  "application/toml",
  "application/yaml",
  "application/x-yaml",
]);

const TEXT_EXTENSION_ALLOWLIST = new Set([
  "md", "txt", "rst", "log",
  "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "env",
  "csv", "tsv", "xml", "html", "htm", "css", "scss", "less",
  "js", "jsx", "ts", "tsx", "mjs", "cjs",
  "py", "rb", "go", "rs", "java", "kt", "swift",
  "c", "cpp", "cc", "h", "hpp", "cs", "php",
  "sh", "bash", "zsh", "fish", "sql", "dockerfile", "gitignore",
]);

interface ChatInputProps {
  onSend: (message: string, attachments?: FileAttachment[]) => void;
  disabled?: boolean;
  placeholder?: string;
  pendingAttachments: PendingAttachment[];
  attachError: string | null;
  onAddFiles: (files: FileList | File[]) => void;
  onRemoveAttachment: (id: string) => void;
  onClearAttachments: () => void;
}

const readAsBase64 = (blob: Blob): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("unexpected reader result"));
        return;
      }
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(blob);
  });

/** Resize an image file to fit within ``MAX_IMAGE_DIMENSION`` on its longest
 *  side and return an ``image`` FileAttachment. GIFs and already-small
 *  images pass through without re-encoding. */
async function prepareImage(file: File): Promise<FileAttachment> {
  const mediaType = file.type;
  if (mediaType === "image/gif") {
    return {
      kind: "image",
      name: file.name,
      media_type: mediaType,
      data: await readAsBase64(file),
    };
  }
  const bitmap = await createImageBitmap(file).catch(() => null);
  if (!bitmap) {
    return {
      kind: "image",
      name: file.name,
      media_type: mediaType,
      data: await readAsBase64(file),
    };
  }
  const longest = Math.max(bitmap.width, bitmap.height);
  if (longest <= MAX_IMAGE_DIMENSION) {
    bitmap.close?.();
    return {
      kind: "image",
      name: file.name,
      media_type: mediaType,
      data: await readAsBase64(file),
    };
  }
  const scale = MAX_IMAGE_DIMENSION / longest;
  const targetW = Math.round(bitmap.width * scale);
  const targetH = Math.round(bitmap.height * scale);
  const canvas = document.createElement("canvas");
  canvas.width = targetW;
  canvas.height = targetH;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    bitmap.close?.();
    return {
      kind: "image",
      name: file.name,
      media_type: mediaType,
      data: await readAsBase64(file),
    };
  }
  ctx.drawImage(bitmap, 0, 0, targetW, targetH);
  bitmap.close?.();
  // Always emit JPEG after a resize — smaller than PNG and the source
  // pixels are already rasterized, so losing the alpha channel is fine.
  const outType = "image/jpeg";
  const blob: Blob = await new Promise((resolve, reject) => {
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error("toBlob failed"))),
      outType,
      JPEG_QUALITY,
    );
  });
  return {
    kind: "image",
    name: file.name,
    media_type: outType,
    data: await readAsBase64(blob),
  };
}

const XLSX_MIME =
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

async function prepareBinaryDocument(
  file: File,
  mediaType: string,
  fallbackName: string,
): Promise<FileAttachment> {
  if (file.size > MAX_DOCUMENT_BYTES) {
    throw new Error(
      `File too large (${Math.round(file.size / 1024 / 1024)} MB > ${MAX_DOCUMENT_BYTES / 1024 / 1024} MB max)`,
    );
  }
  return {
    kind: "document",
    name: file.name || fallbackName,
    media_type: mediaType,
    data: await readAsBase64(file),
  };
}

function looksLikeText(file: File): boolean {
  if (file.type.startsWith("text/")) return true;
  if (TEXT_MIME_ALLOWLIST.has(file.type)) return true;
  const name = file.name.toLowerCase();
  if (name === "dockerfile" || name === "makefile") return true;
  const dot = name.lastIndexOf(".");
  if (dot < 0) return false;
  return TEXT_EXTENSION_ALLOWLIST.has(name.slice(dot + 1));
}

async function prepareText(file: File): Promise<FileAttachment> {
  if (file.size > MAX_TEXT_BYTES) {
    throw new Error(
      `Text file too large (${Math.round(file.size / 1024)} KB > ${MAX_TEXT_BYTES / 1024} KB max)`,
    );
  }
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  // Null-byte sniff — strong signal the file is binary, not text.
  const scanLen = Math.min(bytes.length, 8192);
  for (let i = 0; i < scanLen; i++) {
    if (bytes[i] === 0) {
      throw new Error(`"${file.name}" doesn't look like a text file`);
    }
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error(`"${file.name}" is not valid UTF-8 text`);
  }
  return {
    kind: "text",
    name: file.name || "file.txt",
    media_type: file.type || "text/plain",
    text,
  };
}

/** Classify a dropped/picked file and produce a ``FileAttachment``.
 *  Throws ``Error`` with a user-legible message for anything unsupported. */
export async function prepareChatAttachment(
  file: File,
): Promise<FileAttachment> {
  const name = file.name.toLowerCase();
  if (ALLOWED_IMAGE_TYPES.has(file.type)) return prepareImage(file);
  if (file.type === "application/pdf" || name.endsWith(".pdf")) {
    return prepareBinaryDocument(file, "application/pdf", "document.pdf");
  }
  if (file.type === XLSX_MIME || name.endsWith(".xlsx")) {
    return prepareBinaryDocument(file, XLSX_MIME, "workbook.xlsx");
  }
  if (looksLikeText(file)) return prepareText(file);
  throw new Error(
    `Unsupported file "${file.name || "(unnamed)"}" — attach images, PDFs, spreadsheets, or text files.`,
  );
}

/** Parsed slash-command input broken into the command prefix the user
 *  is typing (possibly empty or partial) and the remainder after it. */
interface SlashParse {
  /** What the user has typed as the command portion so far. May be
   *  ``""`` (bare ``/``), ``"radio"`` (still picking), or ``"radio start"``
   *  (grouped form, fully typed). */
  commandPrefix: string;
  /** Text after the command portion — treated as arguments for the
   *  help strip and positional counting. */
  argsText: string;
  /** True when the user has typed at least one space after the command
   *  portion — i.e. they've committed to whatever prefix is there. */
  committed: boolean;
}

/** Parse the input into a slash-command prefix + args, resolving grouped
 *  forms against the known command list. Returns ``null`` if the input
 *  doesn't start with ``/``.
 *
 *  Algorithm: longest-prefix match. For every registered command
 *  (sorted longest first so ``"radio start"`` wins over ``"radio"``),
 *  check whether the body equals the command or starts with the command
 *  followed by a space. If no full match, the user is still picking,
 *  and we report whatever they've typed (trimmed to ≤ 2 words) as the
 *  prefix so the suggestions list can filter correctly.
 */
function matchSlash(
  input: string,
  knownCommands: readonly string[],
): SlashParse | null {
  if (!input.startsWith("/")) return null;
  const body = input.slice(1);

  // Bare `/` — show the full picker.
  if (body === "") {
    return { commandPrefix: "", argsText: "", committed: false };
  }

  // Longest-prefix match first. Grouped commands (``radio start``) are
  // longer than bare ones (``radio``), so this automatically prefers
  // the grouped form when it's available.
  const sortedCommands = [...knownCommands].sort(
    (a, b) => b.length - a.length,
  );
  for (const cmd of sortedCommands) {
    if (body === cmd || body.startsWith(cmd + " ")) {
      const argsText = body.slice(cmd.length).replace(/^\s+/, "");
      return { commandPrefix: cmd, argsText, committed: true };
    }
  }

  // No full match → still picking. Use at most the first two
  // whitespace-separated tokens so prefixes like ``"radio st"`` narrow
  // grouped suggestions correctly.
  const tokens = body.split(/\s+/).slice(0, 2);
  const partial = tokens.join(" ").trimEnd();
  return { commandPrefix: partial, argsText: "", committed: false };
}

const HISTORY_KEY = "gilbert.chat.history";
const HISTORY_MAX = 100;

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((x): x is string => typeof x === "string")
      : [];
  } catch {
    return [];
  }
}

function saveHistory(history: string[]): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
  } catch {
    // Quota exceeded or storage unavailable — history is best-effort.
  }
}

/** Count shell-style tokens, respecting (simple) quotes, to pick current param. */
function countCompletedTokens(rest: string): number {
  let count = 0;
  let inToken = false;
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < rest.length; i++) {
    const ch = rest[i];
    if (quote) {
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      if (!inToken) {
        inToken = true;
      }
      continue;
    }
    if (ch === " " || ch === "\t") {
      if (inToken) {
        count += 1;
        inToken = false;
      }
      continue;
    }
    inToken = true;
  }
  // If the text ends mid-token (no trailing space), that token is in-progress
  // and counts as the "current" parameter index, not a completed one.
  return count;
}

export function ChatInput({
  onSend,
  disabled = false,
  placeholder = "Type a message...",
  pendingAttachments,
  attachError,
  onAddFiles,
  onRemoveAttachment,
  onClearAttachments,
}: ChatInputProps) {
  const [message, setMessage] = useState("");
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  const [historyIndex, setHistoryIndex] = useState<number>(-1);
  const historyRef = useRef<string[]>(loadHistory());
  const draftRef = useRef<string>("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { connected } = useWebSocket();
  const api = useWsApi();

  const { data: allCommands = [] } = useQuery({
    queryKey: ["slash-commands"],
    queryFn: api.listSlashCommands,
    enabled: connected,
    staleTime: 60_000,
  });

  // Re-focus when enabled (after sending completes)
  useEffect(() => {
    if (!disabled) {
      textareaRef.current?.focus();
    }
  }, [disabled]);

  const knownCommandNames = useMemo(
    () => allCommands.map((c) => c.command),
    [allCommands],
  );

  const slashMatch = useMemo(
    () => matchSlash(message, knownCommandNames),
    [message, knownCommandNames],
  );

  // Pickable commands list: shown while the user is still picking a
  // command (hasn't committed to a known one yet). Filter on the
  // typed prefix, matching against the full command name so grouped
  // commands like ``radio start`` narrow correctly.
  const suggestions = useMemo(() => {
    if (!slashMatch) return [];
    if (slashMatch.committed) return [];
    const prefix = slashMatch.commandPrefix.toLowerCase();
    return allCommands.filter((c) =>
      c.command.toLowerCase().startsWith(prefix),
    );
  }, [slashMatch, allCommands]);

  // Active command (once the user has typed a full known command name).
  const activeCommand = useMemo<SlashCommand | null>(() => {
    if (!slashMatch || !slashMatch.committed) return null;
    return (
      allCommands.find((c) => c.command === slashMatch.commandPrefix) ?? null
    );
  }, [slashMatch, allCommands]);

  // Which parameter is currently being entered, for the help strip.
  const activeParamIndex = useMemo(() => {
    if (!activeCommand || !slashMatch) return -1;
    const tokens = countCompletedTokens(slashMatch.argsText);
    const visibleParams = activeCommand.parameters.filter(
      (p) => !p.name.startsWith("_"),
    );
    return Math.min(tokens, Math.max(0, visibleParams.length - 1));
  }, [activeCommand, slashMatch]);

  // Clamp the suggestion index whenever the list changes
  useEffect(() => {
    if (suggestionIndex >= suggestions.length) {
      setSuggestionIndex(0);
    }
  }, [suggestions.length, suggestionIndex]);

  const handleSend = useCallback(() => {
    const trimmed = message.trim();
    if (!trimmed && pendingAttachments.length === 0) return;
    const outgoing: FileAttachment[] = pendingAttachments.map(
      (p) => p.attachment,
    );
    onSend(trimmed, outgoing);
    const hist = historyRef.current;
    if (trimmed && hist[hist.length - 1] !== trimmed) {
      hist.push(trimmed);
      if (hist.length > HISTORY_MAX) hist.splice(0, hist.length - HISTORY_MAX);
      saveHistory(hist);
    }
    setHistoryIndex(-1);
    draftRef.current = "";
    setMessage("");
    onClearAttachments();
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.focus();
    }
  }, [message, pendingAttachments, onSend, onClearAttachments]);

  const applyHistoryEntry = useCallback((text: string) => {
    setMessage(text);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      const len = el.value.length;
      el.setSelectionRange(len, len);
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 150) + "px";
      el.focus();
    });
  }, []);

  function completeSuggestion(cmd: SlashCommand) {
    const next = `/${cmd.command} `;
    setMessage(next);
    setSuggestionIndex(0);
    // Resize next tick
    requestAnimationFrame(() => {
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
        textareaRef.current.style.height =
          Math.min(textareaRef.current.scrollHeight, 150) + "px";
        textareaRef.current.focus();
      }
    });
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    const pickerOpen = suggestions.length > 0;

    if (pickerOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSuggestionIndex((i) => (i + 1) % suggestions.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSuggestionIndex(
          (i) => (i - 1 + suggestions.length) % suggestions.length,
        );
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        completeSuggestion(suggestions[suggestionIndex]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        // Clear only the leading slash so the picker closes but the
        // user doesn't lose whatever else they were typing.
        setMessage(message.replace(/^\//, ""));
        return;
      }
    }

    const el = textareaRef.current;
    if (el && e.key === "ArrowUp" && historyRef.current.length > 0) {
      const caret = el.selectionStart ?? 0;
      const firstNewline = message.indexOf("\n");
      const onFirstLine = firstNewline === -1 || caret <= firstNewline;
      if (onFirstLine) {
        e.preventDefault();
        let nextIdx: number;
        if (historyIndex === -1) {
          draftRef.current = message;
          nextIdx = historyRef.current.length - 1;
        } else {
          nextIdx = Math.max(0, historyIndex - 1);
        }
        setHistoryIndex(nextIdx);
        applyHistoryEntry(historyRef.current[nextIdx]);
        return;
      }
    }

    if (el && e.key === "ArrowDown" && historyIndex !== -1) {
      const caret = el.selectionStart ?? 0;
      const lastNewline = message.lastIndexOf("\n");
      const onLastLine = lastNewline === -1 || caret > lastNewline;
      if (onLastLine) {
        e.preventDefault();
        const nextIdx = historyIndex + 1;
        if (nextIdx >= historyRef.current.length) {
          setHistoryIndex(-1);
          applyHistoryEntry(draftRef.current);
          draftRef.current = "";
        } else {
          setHistoryIndex(nextIdx);
          applyHistoryEntry(historyRef.current[nextIdx]);
        }
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!disabled) handleSend();
    }
  }

  function handleInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setMessage(e.target.value);
    if (historyIndex !== -1) setHistoryIndex(-1);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 150) + "px";
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const files: File[] = [];
    for (const item of Array.from(e.clipboardData.items)) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      onAddFiles(files);
    }
  }

  function handleFilePick(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files && e.target.files.length > 0) {
      onAddFiles(e.target.files);
    }
    // Reset so the same file can be picked again after removal.
    e.target.value = "";
  }

  // Unknown slash command warning strip. Only fires when the user
  // clearly intended a slash command (typed `/word ` with a space) but
  // the first word doesn't start any known command and the picker is
  // empty. Still-picking prefixes like `/ra` never warn.
  const unknownCommand = useMemo(() => {
    if (!slashMatch) return false;
    if (!slashMatch.commandPrefix) return false; // bare "/" — nothing to judge
    if (allCommands.length === 0) return false; // not loaded yet
    // If the prefix matches any known command (bare or grouped), we're
    // either committed or still picking → no warning.
    const prefix = slashMatch.commandPrefix.toLowerCase();
    const anyMatch = allCommands.some((c) =>
      c.command.toLowerCase().startsWith(prefix),
    );
    if (anyMatch) return false;
    // Otherwise, warn only once the user has committed (typed a space).
    return message.includes(" ");
  }, [slashMatch, message, allCommands]);

  return (
    <div className="shrink-0 border-t bg-background p-3 sm:p-4">
      <div className="relative mx-auto max-w-3xl">
        {/* Autocomplete popover (commands picker) */}
        {suggestions.length > 0 && (
          <div className="absolute bottom-full left-0 right-0 mb-2 max-h-72 overflow-y-auto rounded-md border bg-popover shadow-lg">
            {suggestions.map((cmd, idx) => (
              <button
                key={cmd.command}
                type="button"
                className={`flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left text-sm ${
                  idx === suggestionIndex
                    ? "bg-accent text-foreground"
                    : "text-foreground/90 hover:bg-accent/60"
                }`}
                onMouseEnter={() => setSuggestionIndex(idx)}
                onClick={() => completeSuggestion(cmd)}
              >
                <div className="flex w-full items-center gap-2">
                  <span className="font-mono font-medium">/{cmd.command}</span>
                  <span className="truncate text-xs text-muted-foreground">
                    {cmd.provider}
                  </span>
                </div>
                <div className="line-clamp-2 text-xs text-muted-foreground">
                  {cmd.help || cmd.description}
                </div>
              </button>
            ))}
          </div>
        )}

        {/* Parameter help strip (once a command is selected) */}
        {activeCommand && suggestions.length === 0 && (
          <SlashHelp command={activeCommand} activeIndex={activeParamIndex} />
        )}

        {/* Unknown command warning */}
        {unknownCommand && (
          <div className="mb-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
            Unknown slash command. Press <kbd>/</kbd> to see the list.
          </div>
        )}

        {/* Image attachment error */}
        {attachError && (
          <div className="mb-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
            {attachError}
          </div>
        )}

        {/* Pending attachment strip — image thumbnails for images, chips
            for documents/text. */}
        {pendingAttachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {pendingAttachments.map((p) => (
              <PendingAttachmentCard
                key={p.id}
                item={p}
                onRemove={() => onRemoveAttachment(p.id)}
              />
            ))}
          </div>
        )}

        <div className="flex items-end gap-2">
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,.xlsx,text/*,.md,.txt,.json,.yaml,.yml,.toml,.ini,.log,.csv,.xml,.html,.css,.js,.jsx,.ts,.tsx,.py,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp,.sh,.sql"
            multiple
            className="hidden"
            onChange={handleFilePick}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="shrink-0"
            disabled={
              disabled || pendingAttachments.length >= MAX_CHAT_ATTACHMENTS
            }
            onClick={() => fileInputRef.current?.click()}
            aria-label="Attach files"
          >
            <PaperclipIcon className="size-4" />
          </Button>
          <Textarea
            ref={textareaRef}
            value={message}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={placeholder}
            rows={1}
            className="min-h-[40px] max-h-[150px] resize-none text-base sm:text-sm"
          />
          <Button
            onClick={handleSend}
            disabled={
              disabled || (!message.trim() && pendingAttachments.length === 0)
            }
            size="icon"
            className="shrink-0"
          >
            <SendHorizontalIcon className="size-4" />
            <span className="sr-only">Send</span>
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Inline usage strip shown while the user is filling in a command's args. */
function SlashHelp({
  command,
  activeIndex,
}: {
  command: SlashCommand;
  activeIndex: number;
}) {
  const visibleParams = command.parameters.filter(
    (p) => !p.name.startsWith("_"),
  );
  const currentParam: SlashParameter | undefined = visibleParams[activeIndex];

  return (
    <div className="mb-2 space-y-1 rounded-md border bg-muted/30 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono font-medium">/{command.command}</span>
        {visibleParams.map((p, i) => {
          const active = i === activeIndex;
          const label = p.required ? `<${p.name}>` : `[${p.name}]`;
          return (
            <span
              key={p.name}
              className={`font-mono ${
                active
                  ? "text-foreground font-semibold underline decoration-dotted underline-offset-4"
                  : "text-muted-foreground"
              }`}
            >
              {label}
            </span>
          );
        })}
      </div>
      {currentParam ? (
        <div className="text-muted-foreground">
          <span className="font-mono text-foreground">{currentParam.name}</span>
          <span className="mx-1">·</span>
          <span>{currentParam.type}</span>
          {currentParam.required && (
            <span className="ml-1 text-destructive">(required)</span>
          )}
          {currentParam.description && (
            <>
              <span className="mx-1">—</span>
              <span>{currentParam.description}</span>
            </>
          )}
          {currentParam.enum && currentParam.enum.length > 0 && (
            <div>
              Options:{" "}
              {currentParam.enum.map((v) => (
                <span
                  key={v}
                  className="mr-1 rounded bg-muted px-1 font-mono"
                >
                  {v}
                </span>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="text-muted-foreground italic">
          {command.help || command.description}
        </div>
      )}
    </div>
  );
}

/** Secondary label shown under a chip — e.g. "PDF · 1.2 MB" or "Text · 4 KB".
 *  Pending attachments in the input bar are always inline (the frontend just
 *  base64-encoded them from the user's file), so ``data`` / ``text`` are
 *  present; the defensive fallbacks cover the unified type's optional fields. */
function attachmentLabel(att: FileAttachment): string {
  if (att.kind === "image") {
    return att.media_type.replace(/^image\//, "").toUpperCase();
  }
  if (att.kind === "document") {
    const bytes = Math.floor(((att.data ?? "").length * 3) / 4);
    const label = att.media_type === XLSX_MIME ? "XLSX" : "PDF";
    return `${label} · ${formatBytes(bytes)}`;
  }
  return `Text · ${formatBytes(new Blob([att.text ?? ""]).size)}`;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function PendingAttachmentCard({
  item,
  onRemove,
}: {
  item: PendingAttachment;
  onRemove: () => void;
}) {
  const att = item.attachment;
  if (att.kind === "image" && item.preview) {
    return (
      <div className="group relative size-16 overflow-hidden rounded-md border bg-muted">
        <img
          src={item.preview}
          alt={item.name}
          className="size-full object-cover"
        />
        <RemoveButton name={item.name} onClick={onRemove} />
      </div>
    );
  }
  const Icon = att.kind === "text" ? FileTextIcon : FileIcon;
  return (
    <div className="group relative flex h-16 max-w-xs items-center gap-2 overflow-hidden rounded-md border bg-muted/50 pl-2 pr-6">
      <div className="flex size-10 shrink-0 items-center justify-center rounded bg-muted">
        <Icon className="size-5 text-muted-foreground" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-medium">{item.name}</div>
        <div className="truncate text-[10px] text-muted-foreground">
          {attachmentLabel(att)}
        </div>
      </div>
      <RemoveButton name={item.name} onClick={onRemove} />
    </div>
  );
}

function RemoveButton({ name, onClick }: { name: string; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={`Remove ${name}`}
      onClick={onClick}
      className="absolute right-0.5 top-0.5 rounded-full bg-background/90 p-0.5 text-foreground shadow-sm opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
    >
      <XIcon className="size-3" />
    </button>
  );
}
