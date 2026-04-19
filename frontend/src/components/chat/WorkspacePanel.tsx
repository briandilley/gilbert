import { useState, useEffect, useCallback, useRef } from "react";
import { useEventBus } from "@/hooks/useEventBus";
import {
  ChevronRightIcon,
  DownloadIcon,
  FileIcon,
  FileTextIcon,
  ImageIcon,
  PinIcon,
  PinOffIcon,
  Trash2Icon,
  UploadIcon,
  WrenchIcon,
  PackageIcon,
  GitBranchIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { WorkspaceFile } from "@/types/workspace";

interface WorkspacePanelProps {
  conversationId: string | null;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(1)} KB`;
  return `${bytes} B`;
}

function formatMetadata(file: WorkspaceFile): string {
  const meta = file.metadata || {};
  const parts: string[] = [];

  if (typeof meta.row_count === "number" && typeof meta.column_count === "number") {
    parts.push(`${meta.row_count} rows x ${meta.column_count} cols`);
  }
  if (typeof meta.width === "number" && typeof meta.height === "number") {
    parts.push(`${meta.width}x${meta.height}`);
  }
  if (typeof meta.page_count === "number") {
    parts.push(`${meta.page_count} page${meta.page_count !== 1 ? "s" : ""}`);
  }
  if (typeof meta.line_count === "number" && !meta.row_count && !meta.page_count) {
    parts.push(`${meta.line_count} lines`);
  }
  return parts.join(", ");
}

function fileIcon(mediaType: string) {
  if (mediaType.startsWith("image/")) return <ImageIcon className="size-3.5 text-blue-400 shrink-0" />;
  if (mediaType.startsWith("text/") || mediaType === "application/json") return <FileTextIcon className="size-3.5 text-green-400 shrink-0" />;
  if (mediaType === "application/pdf") return <FileTextIcon className="size-3.5 text-red-400 shrink-0" />;
  return <FileIcon className="size-3.5 text-muted-foreground shrink-0" />;
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

function FileRow({
  file,
  allFiles,
  onDownload,
  onPin,
  onDelete,
}: {
  file: WorkspaceFile;
  allFiles: WorkspaceFile[];
  onDownload: (file: WorkspaceFile) => void;
  onPin: (file: WorkspaceFile) => void;
  onDelete: (file: WorkspaceFile) => void;
}) {
  const metaSummary = formatMetadata(file);

  const parentFile = file.derived_from
    ? allFiles.find((f) => f._id === file.derived_from)
    : null;

  return (
    <div className="group rounded-md px-2 py-1.5 hover:bg-muted/50 transition-colors">
      <div className="flex items-start gap-2">
        {fileIcon(file.media_type)}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1">
            <span className="text-xs font-medium truncate">{file.filename}</span>
            {file.pinned && <PinIcon className="size-2.5 text-amber-500 shrink-0" />}
            {file.reusable && (
              <Badge variant="secondary" className="text-[9px] px-1 py-0 leading-tight">
                reusable
              </Badge>
            )}
          </div>
          <div className="text-[10px] text-muted-foreground truncate">
            {formatBytes(file.size)}
            {metaSummary ? ` \u2022 ${metaSummary}` : ""}
          </div>
          {file.description && (
            <div className="text-[10px] text-muted-foreground/80 italic truncate mt-0.5">
              {file.description}
            </div>
          )}
          {parentFile && (
            <div className="text-[10px] text-muted-foreground/70 flex items-center gap-0.5 mt-0.5">
              <GitBranchIcon className="size-2.5 shrink-0" />
              <span className="truncate">
                from {parentFile.filename}
                {file.derivation_script ? ` via ${file.derivation_script.split("/").pop()}` : ""}
              </span>
            </div>
          )}
        </div>
        <div className="hidden group-hover:flex items-center gap-0.5 shrink-0">
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="ghost"
                  size="xs"
                  className="size-5 p-0"
                  onClick={() => onDownload(file)}
                />
              }
            >
              <DownloadIcon className="size-3" />
            </TooltipTrigger>
            <TooltipContent>Download</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="ghost"
                  size="xs"
                  className="size-5 p-0"
                  onClick={() => onPin(file)}
                />
              }
            >
              {file.pinned
                ? <PinOffIcon className="size-3" />
                : <PinIcon className="size-3" />}
            </TooltipTrigger>
            <TooltipContent>{file.pinned ? "Unpin" : "Pin"}</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="ghost"
                  size="xs"
                  className="size-5 p-0 text-destructive"
                  onClick={() => onDelete(file)}
                />
              }
            >
              <Trash2Icon className="size-3" />
            </TooltipTrigger>
            <TooltipContent>Delete</TooltipContent>
          </Tooltip>
        </div>
      </div>
    </div>
  );
}

function FileSection({
  label,
  icon,
  files,
  allFiles,
  defaultOpen,
  onDownload,
  onPin,
  onDelete,
}: {
  label: string;
  icon: React.ReactNode;
  files: WorkspaceFile[];
  allFiles: WorkspaceFile[];
  defaultOpen: boolean;
  onDownload: (file: WorkspaceFile) => void;
  onPin: (file: WorkspaceFile) => void;
  onDelete: (file: WorkspaceFile) => void;
}) {
  const [open, setOpen] = useState(defaultOpen);

  if (files.length === 0) return null;

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-1.5 px-2 py-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronRightIcon
          className={cn("size-3 transition-transform", open && "rotate-90")}
        />
        {icon}
        <span>{label}</span>
        <span className="ml-auto text-[10px] font-normal tabular-nums">
          {files.length}
        </span>
      </button>
      {open && (
        <div className="space-y-0.5 mt-0.5">
          {files.map((file) => (
            <FileRow
              key={file._id}
              file={file}
              allFiles={allFiles}
              onDownload={onDownload}
              onPin={onPin}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function WorkspacePanelContent({ conversationId }: WorkspacePanelProps) {
  const api = useWsApi();
  const { connected } = useWebSocket();

  const [uploads, setUploads] = useState<WorkspaceFile[]>([]);
  const [outputs, setOutputs] = useState<WorkspaceFile[]>([]);
  const [scratch, setScratch] = useState<WorkspaceFile[]>([]);
  const [loading, setLoading] = useState(false);

  const allFiles = [...uploads, ...outputs, ...scratch];

  const loadFiles = useCallback(async () => {
    if (!conversationId || !connected) return;
    setLoading(true);
    try {
      const result = await api.listWorkspaceFiles(conversationId);
      setUploads(result.uploads || []);
      setOutputs(result.outputs || []);
      setScratch(result.scratch || []);
    } catch {
      // Workspace service may not be available
    } finally {
      setLoading(false);
    }
  }, [conversationId, connected, api]);

  useEffect(() => {
    loadFiles();
  }, [loadFiles]);

  // Reload when workspace file events arrive for this conversation
  const loadFilesRef = useRef(loadFiles);
  loadFilesRef.current = loadFiles;

  const handleFileEvent = useCallback(
    (event: { data?: { conversation_id?: string } }) => {
      if (
        event.data?.conversation_id === conversationId
      ) {
        loadFilesRef.current();
      }
    },
    [conversationId],
  );
  useEventBus("workspace.file.created", handleFileEvent);
  useEventBus("workspace.file.deleted", handleFileEvent);

  const handleDownload = useCallback(async (file: WorkspaceFile) => {
    if (!conversationId) return;
    try {
      const result = await api.downloadWorkspaceFile(
        file.rel_path,
        conversationId,
      );
      const buffer = base64ToArrayBuffer(result.content_base64);
      const blob = new Blob([buffer], {
        type: result.media_type || "application/octet-stream",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result.filename || file.filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      // Download failed
    }
  }, [conversationId, api]);

  const handlePin = useCallback(async (file: WorkspaceFile) => {
    try {
      await api.pinWorkspaceFile(file._id, !file.pinned);
      await loadFiles();
    } catch {
      // Pin failed
    }
  }, [api, loadFiles]);

  const handleDelete = useCallback(async (file: WorkspaceFile) => {
    try {
      await api.deleteWorkspaceFile(file._id);
      await loadFiles();
    } catch {
      // Delete failed
    }
  }, [api, loadFiles]);

  const hasFiles = uploads.length > 0 || outputs.length > 0 || scratch.length > 0;

  return (
    <ScrollArea className="h-full">
      <div className="p-3">
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground mb-3 px-1">
          Workspace Files
        </h3>

        {loading && !hasFiles && (
          <p className="text-xs text-muted-foreground px-2">Loading...</p>
        )}

        {!loading && !hasFiles && (
          <div className="text-center py-6 text-muted-foreground">
            <PackageIcon className="size-8 mx-auto mb-2 opacity-20" />
            <p className="text-xs">No files yet</p>
            <p className="text-[10px] mt-1">
              Upload files or ask Gilbert to generate something
            </p>
          </div>
        )}

        {hasFiles && (
          <div className="space-y-2">
            <FileSection
              label="Uploads"
              icon={<UploadIcon className="size-3" />}
              files={uploads}
              allFiles={allFiles}
              defaultOpen={true}
              onDownload={handleDownload}
              onPin={handlePin}
              onDelete={handleDelete}
            />
            <FileSection
              label="Outputs"
              icon={<PackageIcon className="size-3" />}
              files={outputs}
              allFiles={allFiles}
              defaultOpen={true}
              onDownload={handleDownload}
              onPin={handlePin}
              onDelete={handleDelete}
            />
            <FileSection
              label="Working Files"
              icon={<WrenchIcon className="size-3" />}
              files={scratch}
              allFiles={allFiles}
              defaultOpen={false}
              onDownload={handleDownload}
              onPin={handlePin}
              onDelete={handleDelete}
            />
          </div>
        )}
      </div>
    </ScrollArea>
  );
}
