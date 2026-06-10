import { useState, useEffect, useCallback, useMemo } from "react";
import {
  ChevronRightIcon,
  DownloadIcon,
  FileIcon,
  FileTextIcon,
  ImageIcon,
  XIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { useWsApi } from "@/hooks/useWsApi";
import hljs from "highlight.js/lib/core";
import { MarkdownContent } from "@/components/ui/MarkdownContent";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { WorkspaceFile } from "@/types/workspace";

interface FileViewerProps {
  open: boolean;
  onClose: () => void;
  files: WorkspaceFile[];
  initialFile?: WorkspaceFile;
  conversationId: string;
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

function formatBytes(bytes: number): string {
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(1)} KB`;
  return `${bytes} B`;
}

function fileIcon(mediaType: string) {
  if (mediaType.startsWith("image/"))
    return <ImageIcon className="size-3.5 text-blue-400 shrink-0" />;
  if (
    mediaType.startsWith("text/") ||
    mediaType === "application/json" ||
    mediaType === "application/javascript"
  )
    return <FileTextIcon className="size-3.5 text-green-400 shrink-0" />;
  if (mediaType === "application/pdf")
    return <FileTextIcon className="size-3.5 text-red-400 shrink-0" />;
  return <FileIcon className="size-3.5 text-muted-foreground shrink-0" />;
}

function getLanguageFromFilename(filename: string): string | null {
  const ext = filename.split(".").pop()?.toLowerCase() || "";
  const map: Record<string, string> = {
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "typescript",
    py: "python",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    css: "css",
    html: "html",
    xml: "xml",
    sql: "sql",
    md: "markdown",
    diff: "diff",
    csv: "plaintext",
    txt: "plaintext",
    log: "plaintext",
    cfg: "plaintext",
    ini: "plaintext",
    toml: "plaintext",
    env: "plaintext",
  };
  return map[ext] || null;
}

function isTextFile(mediaType: string, filename: string): boolean {
  if (mediaType.startsWith("text/")) return true;
  if (
    [
      "application/json",
      "application/javascript",
      "application/xml",
      "application/x-yaml",
      "application/toml",
    ].includes(mediaType)
  )
    return true;
  const ext = filename.split(".").pop()?.toLowerCase() || "";
  return [
    "js", "jsx", "ts", "tsx", "py", "sh", "bash", "json", "yaml", "yml",
    "css", "html", "xml", "sql", "md", "diff", "csv", "txt", "log", "cfg",
    "ini", "toml", "env", "rb", "rs", "go", "java", "c", "cpp", "h", "hpp",
    "r", "lua", "php", "swift", "kt", "scala", "pl", "pm",
  ].includes(ext);
}

function isImageFile(mediaType: string): boolean {
  return mediaType.startsWith("image/");
}

function isVideoFile(mediaType: string): boolean {
  return mediaType.startsWith("video/");
}

function isAudioFile(mediaType: string): boolean {
  return mediaType.startsWith("audio/");
}

function isPdfFile(mediaType: string): boolean {
  return mediaType === "application/pdf";
}

function HexView({ data }: { data: ArrayBuffer }) {
  const bytes = new Uint8Array(data);
  const maxBytes = Math.min(bytes.length, 4096);
  const lines: string[] = [];

  for (let offset = 0; offset < maxBytes; offset += 16) {
    const hex: string[] = [];
    const ascii: string[] = [];
    for (let i = 0; i < 16; i++) {
      if (offset + i < maxBytes) {
        const b = bytes[offset + i];
        hex.push(b.toString(16).padStart(2, "0"));
        ascii.push(b >= 0x20 && b <= 0x7e ? String.fromCharCode(b) : ".");
      } else {
        hex.push("  ");
        ascii.push(" ");
      }
    }
    const addr = offset.toString(16).padStart(8, "0");
    lines.push(
      `${addr}  ${hex.slice(0, 8).join(" ")}  ${hex.slice(8).join(" ")}  |${ascii.join("")}|`,
    );
  }

  if (bytes.length > maxBytes) {
    lines.push(`\n... ${bytes.length - maxBytes} more bytes not shown ...`);
  }

  return (
    <pre className="font-mono text-xs leading-5 text-muted-foreground whitespace-pre overflow-x-auto p-4">
      {lines.join("\n")}
    </pre>
  );
}

function FilePreview({
  file,
  conversationId,
}: {
  file: WorkspaceFile;
  conversationId: string;
}) {
  const api = useWsApi();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [textContent, setTextContent] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [rawBuffer, setRawBuffer] = useState<ArrayBuffer | null>(null);
  // Markdown files default to a rendered view (not syntax-highlighted source).
  const [mdView, setMdView] = useState<"rendered" | "raw">("rendered");

  const mt = file.media_type;
  const isText = isTextFile(mt, file.filename);
  const isImage = isImageFile(mt);
  const isVideo = isVideoFile(mt);
  const isAudio = isAudioFile(mt);
  const isPdf = isPdfFile(mt);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      setTextContent(null);
      setBlobUrl(null);
      setRawBuffer(null);

      try {
        const result = await api.downloadWorkspaceFile(
          file.rel_path,
          conversationId,
        );
        if (cancelled) return;

        const buffer = base64ToArrayBuffer(result.content_base64);

        if (isText) {
          const decoder = new TextDecoder("utf-8", { fatal: false });
          setTextContent(decoder.decode(buffer));
        } else if (isImage || isVideo || isAudio || isPdf) {
          const blob = new Blob([buffer], {
            type: result.media_type || mt,
          });
          setBlobUrl(URL.createObjectURL(blob));
        } else {
          setRawBuffer(buffer);
        }
      } catch {
        if (!cancelled) setError("Failed to load file");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => {
      cancelled = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file._id, file.rel_path, conversationId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <p className="text-sm">Loading...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full text-destructive">
        <p className="text-sm">{error}</p>
      </div>
    );
  }

  if (textContent !== null) {
    // Markdown files render as formatted markdown (with a Raw toggle), not as
    // syntax-highlighted source — that's how a report is meant to be read.
    const isMarkdown = /\.(md|markdown)$/i.test(file.filename);
    if (isMarkdown) {
      return (
        <ScrollArea className="h-full">
          <div className="p-4">
            <Tabs
              value={mdView}
              onValueChange={(v) => setMdView((v as "rendered" | "raw") || "rendered")}
            >
              <TabsList variant="line">
                <TabsTrigger value="rendered">Rendered</TabsTrigger>
                <TabsTrigger value="raw">Raw</TabsTrigger>
              </TabsList>
            </Tabs>
            <div className="mt-3">
              {mdView === "raw" ? (
                <pre className="text-xs leading-5 whitespace-pre-wrap break-words font-mono text-foreground/90">
                  {textContent}
                </pre>
              ) : (
                <MarkdownContent content={textContent} />
              )}
            </div>
          </div>
        </ScrollArea>
      );
    }

    const lang = getLanguageFromFilename(file.filename);
    let highlighted = false;
    let html = "";
    if (lang && lang !== "plaintext" && hljs.getLanguage(lang)) {
      try {
        const result = hljs.highlight(textContent, { language: lang });
        html = result.value;
        highlighted = true;
      } catch {
        // fall through
      }
    }

    return (
      <ScrollArea className="h-full">
        <div className="p-4">
          {highlighted ? (
            <pre className="text-xs leading-5 overflow-x-auto">
              <code
                className={`hljs language-${lang}`}
                dangerouslySetInnerHTML={{ __html: html }}
              />
            </pre>
          ) : (
            <pre className="text-xs leading-5 text-foreground/90 whitespace-pre-wrap overflow-x-auto font-mono">
              {textContent}
            </pre>
          )}
        </div>
      </ScrollArea>
    );
  }

  if (isImage && blobUrl) {
    return (
      <div className="flex items-center justify-center h-full p-4 overflow-auto">
        <img
          src={blobUrl}
          alt={file.filename}
          className="max-w-full max-h-full object-contain rounded"
        />
      </div>
    );
  }

  if (isVideo && blobUrl) {
    return (
      <div className="flex items-center justify-center h-full p-4">
        <video
          src={blobUrl}
          controls
          className="max-w-full max-h-full rounded"
        />
      </div>
    );
  }

  if (isAudio && blobUrl) {
    return (
      <div className="flex items-center justify-center h-full p-4">
        <audio src={blobUrl} controls className="w-full max-w-md" />
      </div>
    );
  }

  if (isPdf && blobUrl) {
    return (
      <iframe
        src={blobUrl}
        title={file.filename}
        className="w-full h-full border-0"
      />
    );
  }

  if (rawBuffer) {
    return (
      <ScrollArea className="h-full">
        <HexView data={rawBuffer} />
      </ScrollArea>
    );
  }

  return (
    <div className="flex items-center justify-center h-full text-muted-foreground">
      <p className="text-sm">Cannot preview this file type</p>
    </div>
  );
}

function FileListItem({
  file,
  selected,
  onClick,
}: {
  file: WorkspaceFile;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "w-full flex items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors",
        selected
          ? "bg-accent text-accent-foreground"
          : "hover:bg-muted/50 text-foreground/80",
      )}
    >
      {fileIcon(file.media_type)}
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium truncate">{file.filename}</div>
        <div className="text-[10px] text-muted-foreground">
          {formatBytes(file.size)}
        </div>
      </div>
    </button>
  );
}

export function FileViewerModal({
  open,
  onClose,
  files,
  initialFile,
  conversationId,
}: FileViewerProps) {
  const [selectedFile, setSelectedFile] = useState<WorkspaceFile | null>(
    initialFile || null,
  );
  const api = useWsApi();

  useEffect(() => {
    if (open && initialFile) {
      setSelectedFile(initialFile);
    }
  }, [open, initialFile]);

  useEffect(() => {
    if (open && !selectedFile && files.length > 0) {
      setSelectedFile(files[0]);
    }
  }, [open, selectedFile, files]);

  const grouped = useMemo(() => {
    const uploads = files.filter((f) => f.category === "upload");
    const outputs = files.filter((f) => f.category === "output");
    const scratch = files.filter((f) => f.category === "scratch");
    return { uploads, outputs, scratch };
  }, [files]);

  const handleDownload = useCallback(async () => {
    if (!selectedFile) return;
    try {
      const result = await api.downloadWorkspaceFile(
        selectedFile.rel_path,
        conversationId,
      );
      const buffer = base64ToArrayBuffer(result.content_base64);
      const blob = new Blob([buffer], {
        type: result.media_type || "application/octet-stream",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result.filename || selectedFile.filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      // Download failed
    }
  }, [selectedFile, conversationId, api]);

  useEffect(() => {
    if (!open) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal */}
      <div className="relative z-10 flex m-4 w-full rounded-xl bg-popover ring-1 ring-foreground/10 overflow-hidden shadow-2xl">
        {/* File list sidebar */}
        <div className="w-56 shrink-0 border-r flex flex-col">
          <div className="flex items-center justify-between px-3 py-2 border-b">
            <h3 className="text-xs font-medium">Files</h3>
          </div>
          <ScrollArea className="flex-1">
            <div className="p-2 space-y-3">
              {grouped.uploads.length > 0 && (
                <FileGroup
                  label="Uploads"
                  files={grouped.uploads}
                  selectedFile={selectedFile}
                  onSelect={setSelectedFile}
                />
              )}
              {grouped.outputs.length > 0 && (
                <FileGroup
                  label="Outputs"
                  files={grouped.outputs}
                  selectedFile={selectedFile}
                  onSelect={setSelectedFile}
                />
              )}
              {grouped.scratch.length > 0 && (
                <FileGroup
                  label="Working Files"
                  files={grouped.scratch}
                  selectedFile={selectedFile}
                  onSelect={setSelectedFile}
                />
              )}
            </div>
          </ScrollArea>
        </div>

        {/* Preview area */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Header */}
          <div className="flex items-center gap-2 px-4 py-2 border-b shrink-0">
            {selectedFile && (
              <>
                {fileIcon(selectedFile.media_type)}
                <span className="text-sm font-medium truncate flex-1">
                  {selectedFile.filename}
                </span>
                <span className="text-xs text-muted-foreground">
                  {formatBytes(selectedFile.size)}
                </span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={handleDownload}
                >
                  <DownloadIcon className="size-4" />
                </Button>
              </>
            )}
            <Button variant="ghost" size="icon-sm" onClick={onClose}>
              <XIcon className="size-4" />
            </Button>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-hidden">
            {selectedFile ? (
              <FilePreview
                key={selectedFile._id}
                file={selectedFile}
                conversationId={conversationId}
              />
            ) : (
              <div className="flex items-center justify-center h-full text-muted-foreground">
                <p className="text-sm">Select a file to preview</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function FileGroup({
  label,
  files,
  selectedFile,
  onSelect,
}: {
  label: string;
  files: WorkspaceFile[];
  selectedFile: WorkspaceFile | null;
  onSelect: (file: WorkspaceFile) => void;
}) {
  const [open, setOpen] = useState(true);

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-1 px-1 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronRightIcon
          className={cn("size-2.5 transition-transform", open && "rotate-90")}
        />
        <span>{label}</span>
        <span className="ml-auto tabular-nums">{files.length}</span>
      </button>
      {open && (
        <div className="space-y-0.5 mt-0.5">
          {files.map((file) => (
            <FileListItem
              key={file._id}
              file={file}
              selected={selectedFile?._id === file._id}
              onClick={() => onSelect(file)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
