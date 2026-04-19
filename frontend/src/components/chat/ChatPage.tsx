import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/hooks/useAuth";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type {
  ChatRound,
  ChatRoundTool,
  ChatTurn,
  FileAttachment,
} from "@/types/chat";
import type { GilbertEvent } from "@/types/events";
import type { UIBlock } from "@/types/ui";
import { ChatSidebarContent } from "./ChatSidebar";
import { MessageList } from "./MessageList";
import {
  ChatInput,
  prepareChatAttachment,
  type PendingAttachment,
} from "./ChatInput";
import { MemberPanelContent } from "./MemberPanel";
import { WorkspacePanelContent } from "./WorkspacePanel";
import { InviteModal } from "./InviteModal";
import { SkillsModal } from "./SkillsModal";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  FolderOpenIcon,
  MenuIcon,
  MessageSquareIcon,
  PlusIcon,
  SparklesIcon,
  UserPlusIcon,
  UsersRoundIcon,
} from "lucide-react";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { PromptDialog } from "@/components/ui/PromptDialog";

export function ChatPage() {
  const { user } = useAuth();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [uiBlocks, setUiBlocks] = useState<UIBlock[]>([]);
  const [sending, setSending] = useState(false);
  const [loadingConv, setLoadingConv] = useState(false);
  const [isShared, setIsShared] = useState(false);
  const [members, setMembers] = useState<
    { user_id: string; display_name: string; role?: "owner" | "member" }[]
  >([]);
  const [ownerId, setOwnerId] = useState<string>("");
  const [roomTitle, setRoomTitle] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [promptDialog, setPromptDialog] = useState<{
    title: string;
    placeholder?: string;
    defaultValue?: string;
    submitLabel?: string;
    onSubmit: (value: string) => void;
  } | null>(null);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [allUsers, setAllUsers] = useState<{ user_id: string; display_name: string }[]>([]);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [pendingInvites, setPendingInvites] = useState<{ user_id: string; display_name: string }[]>([]);
  const [inviteResponseDialog, setInviteResponseDialog] = useState<{
    conversationId: string;
    title: string;
  } | null>(null);
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [modelSelection, setModelSelection] = useState<{ backend: string; model: string }>({
    backend: "",
    model: "",
  });
  const [dragActive, setDragActive] = useState(false);
  const dragDepthRef = useRef(0);
  const pendingCountRef = useRef(0);
  const chatAreaRef = useRef<HTMLDivElement>(null);
  const activeConvIdRef = useRef<string | null>(null);
  // Tracks "the next text_delta should open a new round." Set when
  // chat.stream.round_complete fires for the in-flight turn; cleared
  // when the next text_delta consumes it. Lives outside React state
  // because it has to be read+written from event handlers without
  // racing the setTurns batch.
  const nextRoundPendingRef = useRef(false);
  // RPC ref of the currently in-flight ``chat.message.send``. Set by
  // ``handleSend`` before awaiting the promise, cleared on resolution
  // (success or error). The stop button reads this to know what to
  // cancel via ``api.cancelMessage(ref)``.
  const inFlightSendRef = useRef<string | null>(null);
  // Keep the ref in sync so streaming event handlers can read the
  // current conversation id without re-registering on every change.
  useEffect(() => {
    activeConvIdRef.current = activeConvId;
  }, [activeConvId]);

  // Keep pendingCountRef in sync so addFiles can read the current count
  // without taking pendingAttachments as a callback dep (that would
  // cause the drop listener to tear down on every attach).
  useEffect(() => {
    pendingCountRef.current = pendingAttachments.length;
  }, [pendingAttachments.length]);

  // Release any blob URLs we're still holding when the page unmounts.
  useEffect(() => {
    return () => {
      for (const p of pendingAttachments) {
        if (p.preview) URL.revokeObjectURL(p.preview);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files);
      if (list.length === 0) return;
      setAttachError(null);
      const toAdd = list;

      // Two-phase flow:
      //
      // 1. Classify each file. Inline attachments (images, small
      //    PDFs, text) get a resolved FileAttachment immediately
      //    and are added to the pending list as normal. Upload-path
      //    files (large / generic) are added as placeholder chips
      //    in "uploading" state — their ``attachment`` field is
      //    null until the upload resolves. The placeholder keeps
      //    the chip count correct so the 8-attachment cap works
      //    mid-upload.
      // 2. Kick off the uploads in the background. Each upload
      //    streams the file to ``/api/chat/upload`` with progress
      //    events wired to the pending entry; on success the
      //    entry's ``attachment`` gets filled in and ``uploading``
      //    flips to false. On failure the entry gets an ``error``
      //    label and stays in place so the user can retry or
      //    dismiss it.
      //
      // Large uploads also need a conversation_id — the endpoint
      // uses it to scope the workspace directory. For a brand-new
      // chat we lazily create the conversation now (via
      // ``chat.conversation.create``) and pin ``activeConvId`` so
      // the eventual send goes into the same row.
      type Pending = PendingAttachment & { _uploadFile?: File };
      const newPending: Pending[] = [];

      for (const file of toAdd) {
        const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        try {
          const prepared = await prepareChatAttachment(file);
          newPending.push({
            id,
            name: file.name || "file",
            attachment: null,
            uploading: true,
            progress: 0,
            _uploadFile: prepared.file,
          });
        } catch (exc) {
          setAttachError(
            exc instanceof Error ? exc.message : "Failed to read file",
          );
        }
      }

      if (newPending.length === 0) return;

      // Commit the placeholder+inline chips to state up-front so
      // the UI shows them immediately.
      setPendingAttachments((prev) => [
        ...prev,
        ...newPending.map((p) => {
          const { _uploadFile: _drop, ...clean } = p;
          void _drop;
          return clean as PendingAttachment;
        }),
      ]);

      // Now kick off any required uploads.
      const uploadEntries = newPending.filter((p) => p._uploadFile);
      if (uploadEntries.length === 0) return;

      // Ensure we have a conversation_id for the upload endpoint.
      // If this is a fresh chat, create one now so the eager
      // upload has somewhere to land; if the user abandons without
      // sending, the empty conversation sticks around and they
      // can delete it.
      //
      // We deliberately don't refetch the conversation list here —
      // handleSend will refetch after the first message, at which
      // point the sidebar picks it up. Refetching mid-upload would
      // cause the sidebar to flash for every attachment and also
      // creates a forward-reference to ``refetchConversations``
      // (which is declared via useQuery further down in this
      // component).
      let convId = activeConvId;
      if (!convId) {
        try {
          const created = await api.createConversation("New conversation");
          convId = created.conversation_id;
          setActiveConvId(convId);
        } catch (exc) {
          // Creation failed — mark every uploading placeholder as
          // errored so the user sees what happened.
          const detail =
            exc instanceof Error ? exc.message : "Failed to create conversation";
          setPendingAttachments((prev) =>
            prev.map((p) =>
              uploadEntries.some((u) => u.id === p.id)
                ? { ...p, uploading: false, error: detail }
                : p,
            ),
          );
          return;
        }
      }

      // Kick off the uploads in parallel. Each one updates its own
      // pending entry in place as progress events arrive and when
      // the upload resolves / rejects.
      for (const entry of uploadEntries) {
        const file = entry._uploadFile!;
        api
          .uploadChatFile(convId!, file, (loaded, total) => {
            setPendingAttachments((prev) =>
              prev.map((p) =>
                p.id === entry.id
                  ? { ...p, progress: total > 0 ? loaded / total : 0 }
                  : p,
              ),
            );
          })
          .then((attachment) => {
            setPendingAttachments((prev) =>
              prev.map((p) =>
                p.id === entry.id
                  ? {
                      ...p,
                      attachment,
                      uploading: false,
                      progress: 1,
                    }
                  : p,
              ),
            );
          })
          .catch((exc: unknown) => {
            const detail =
              exc instanceof Error ? exc.message : "Upload failed";
            setPendingAttachments((prev) =>
              prev.map((p) =>
                p.id === entry.id
                  ? { ...p, uploading: false, error: detail }
                  : p,
              ),
            );
          });
      }
    },
    [activeConvId, api],
  );

  const removeAttachment = useCallback((id: string) => {
    setPendingAttachments((prev) => {
      const victim = prev.find((p) => p.id === id);
      if (victim?.preview) URL.revokeObjectURL(victim.preview);
      return prev.filter((p) => p.id !== id);
    });
  }, []);

  const clearAttachments = useCallback(() => {
    setPendingAttachments((prev) => {
      for (const p of prev) {
        if (p.preview) URL.revokeObjectURL(p.preview);
      }
      return [];
    });
    setAttachError(null);
  }, []);

  // Direct DOM event listeners on the chat area so drag/drop behaves
  // the same regardless of which child element the cursor is over.
  // (Earlier this used React synthetic-event handlers on the wrapper
  // div, and drops on the textarea weren't reliably reaching them.)
  useEffect(() => {
    const el = chatAreaRef.current;
    if (!el) return;

    const hasFiles = (e: DragEvent) =>
      Array.from(e.dataTransfer?.types ?? []).includes("Files");

    const onDragEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepthRef.current += 1;
      setDragActive(true);
    };
    const onDragOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      // Must preventDefault on dragover for drop events to fire.
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const onDragLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
      if (dragDepthRef.current === 0) setDragActive(false);
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      dragDepthRef.current = 0;
      setDragActive(false);
      const files = Array.from(e.dataTransfer?.files ?? []);
      // addFiles handles classification and user-visible errors for
      // unsupported types — don't pre-filter here.
      if (files.length > 0) void addFiles(files);
    };

    el.addEventListener("dragenter", onDragEnter);
    el.addEventListener("dragover", onDragOver);
    el.addEventListener("dragleave", onDragLeave);
    el.addEventListener("drop", onDrop);
    return () => {
      el.removeEventListener("dragenter", onDragEnter);
      el.removeEventListener("dragover", onDragOver);
      el.removeEventListener("dragleave", onDragLeave);
      el.removeEventListener("drop", onDrop);
    };
  }, [addFiles]);

  const { data: conversations = [], refetch: refetchConversations } = useQuery({
    queryKey: ["conversations"],
    queryFn: api.listConversations,
    enabled: connected,
  });

  const { data: modelsData } = useQuery({
    queryKey: ["chat-models"],
    queryFn: api.listModels,
    enabled: connected,
  });

  const loadConversation = useCallback(
    async (id: string) => {
      setLoadingConv(true);
      try {
        const conv = await api.loadConversation(id);
        setActiveConvId(id);
        setTurns(conv.turns || []);
        setUiBlocks(conv.ui_blocks || []);
        setIsShared(conv.shared);
        setMembers(
          (conv.members || []).map((m) => ({
            ...m,
            role: m.role as "owner" | "member" | undefined,
          })),
        );
        setPendingInvites(conv.invites || []);
        setOwnerId(conv.owner_id || "");
        setRoomTitle(conv.title);
        if (conv.model_preference) {
          setModelSelection(conv.model_preference);
        } else {
          setModelSelection({ backend: "", model: "" });
        }
        setSidebarOpen(false);
      } catch {
        setActiveConvId(null);
      } finally {
        setLoadingConv(false);
      }
    },
    [api],
  );

  const handleSend = useCallback(
    async (message: string, attachments: FileAttachment[] = []) => {
      // Insert a streaming placeholder turn at the bottom of the list.
      // Stream events from the server (text_delta, tool.started,
      // tool.completed) mutate this turn's rounds in place; the
      // chat.message.send RPC result replaces it with the authoritative
      // committed shape.
      nextRoundPendingRef.current = false;
      setTurns((prev) => [
        ...prev,
        {
          user_message: {
            content: message,
            attachments,
          },
          rounds: [],
          final_content: "",
          final_attachments: [],
          incomplete: false,
          streaming: true,
        },
      ]);
      setSending(true);

      // Send via the tracked variant so we capture the RPC ref — the
      // stop button uses it to send a matching ``chat.message.cancel``.
      const modelOpts = (modelSelection.model || modelSelection.backend)
        ? { model: modelSelection.model, backend: modelSelection.backend }
        : undefined;
      const { ref, promise } = api.sendMessageWithRef(
        message,
        activeConvId,
        attachments,
        modelOpts,
      );
      inFlightSendRef.current = ref;

      try {
        const resp = await promise;
        setActiveConvId(resp.conversation_id);

        // Replace the streaming placeholder with the authoritative
        // committed turn shape. The server's ``rounds`` is the source
        // of truth; the live-built rounds we accumulated from stream
        // events get thrown away in favor of the persisted version.
        // ``interrupted`` rides through so TurnBubble can render the
        // subtle stop icon on the committed turn.
        setTurns((prev) => {
          const next = [...prev];
          const lastIdx = next.length - 1;
          if (lastIdx >= 0 && next[lastIdx].streaming) {
            next[lastIdx] = {
              user_message: next[lastIdx].user_message,
              rounds: resp.rounds ?? [],
              final_content: resp.response ?? "",
              final_attachments: resp.attachments ?? [],
              incomplete: false,
              interrupted: resp.interrupted === true,
              streaming: false,
            };
          }
          return next;
        });

        if (resp.ui_blocks?.length) {
          setUiBlocks((prev) => [...prev, ...resp.ui_blocks]);
        }

        refetchConversations();
      } catch (exc) {
        const detail =
          exc instanceof Error && exc.message ? exc.message : String(exc);
        // Replace the streaming placeholder with an error turn so the
        // user sees what happened in context, instead of a ghost
        // bubble that vanishes.
        setTurns((prev) => {
          const next = [...prev];
          const lastIdx = next.length - 1;
          if (lastIdx >= 0 && next[lastIdx].streaming) {
            next[lastIdx] = {
              user_message: next[lastIdx].user_message,
              rounds: next[lastIdx].rounds,
              final_content: `Sorry, something went wrong. Please try again.\n\n\`${detail}\``,
              final_attachments: [],
              incomplete: false,
              streaming: false,
            };
          }
          return next;
        });
      } finally {
        inFlightSendRef.current = null;
        setSending(false);
      }
    },
    [api, activeConvId, refetchConversations, modelSelection],
  );

  const handleStop = useCallback(async () => {
    // Click-to-interrupt for the stop button. Fires a
    // ``chat.message.cancel`` against the currently tracked send ref;
    // the backend cancels the in-flight task, ``AIService.chat()``
    // catches ``CancelledError`` and persists partial state, and the
    // awaiting ``handleSend`` resolves with ``interrupted=true``. The
    // per-turn visual update flows through the normal resolve path.
    const ref = inFlightSendRef.current;
    if (!ref) return;
    try {
      await api.cancelMessage(ref);
    } catch {
      // Cancel failed (404 — turn already finished, 403 — not the
      // originator in a shared room, or network blip). The
      // in-flight promise will resolve on its own; nothing else to do.
    }
  }, [api]);

  const handleBlockSubmit = useCallback(
    async (blockId: string, values: Record<string, unknown>) => {
      if (!activeConvId) return;

      setUiBlocks((prev) =>
        prev.map((b) =>
          b.block_id === blockId
            ? { ...b, submitted: true, submission: values }
            : b,
        ),
      );

      setSending(true);
      try {
        const resp = await api.submitForm(activeConvId, blockId, values);
        if (
          resp.response ||
          resp.attachments?.length ||
          resp.rounds?.length
        ) {
          // Form submission produces a synthetic user→assistant turn
          // without a real user message. The user side carries the
          // form values as a placeholder so the turn renders cleanly.
          setTurns((prev) => [
            ...prev,
            {
              user_message: { content: "", attachments: [] },
              rounds: resp.rounds ?? [],
              final_content: resp.response ?? "",
              final_attachments: resp.attachments ?? [],
              incomplete: false,
              streaming: false,
            },
          ]);
        }
        if (resp.ui_blocks?.length) {
          setUiBlocks((prev) => [...prev, ...resp.ui_blocks]);
        }
      } finally {
        setSending(false);
      }
    },
    [api, activeConvId],
  );

  const clearChat = useCallback(() => {
    setActiveConvId(null);
    setTurns([]);
    setUiBlocks([]);
    setIsShared(false);
    setMembers([]);
    setPendingInvites([]);
    setOwnerId("");
    setRoomTitle("");
    setSidebarOpen(false);
  }, []);

  const handleNewChat = useCallback(() => {
    setPromptDialog({
      title: "New Chat",
      placeholder: "Chat name",
      submitLabel: "Create",
      onSubmit: async (name) => {
        setPromptDialog(null);
        try {
          const result = await api.createConversation(name.trim() || "New conversation");
          refetchConversations();
          setActiveConvId(result.conversation_id);
          setTurns([]);
          setUiBlocks([]);
          setIsShared(false);
          setMembers([]);
          setRoomTitle(result.title);
        } catch {
          // ignore
        }
      },
    });
  }, [api, refetchConversations]);

  const handleCreateRoom = useCallback(() => {
    setPromptDialog({
      title: "New Room",
      placeholder: "Room name",
      submitLabel: "Create",
      onSubmit: async (title) => {
        setPromptDialog(null);
        const room = await api.createRoom(title);
        refetchConversations();
        loadConversation(room.conversation_id);
      },
    });
  }, [api, refetchConversations, loadConversation]);

  const handleJoinRoom = useCallback(
    async (id: string) => {
      await api.joinRoom(id);
      refetchConversations();
      loadConversation(id);
    },
    [api, refetchConversations, loadConversation],
  );

  const handleLeaveRoom = useCallback(
    async (id: string) => {
      await api.leaveRoom(id);
      if (activeConvId === id) clearChat();
      refetchConversations();
    },
    [api, activeConvId, clearChat, refetchConversations],
  );

  const handleKick = useCallback(
    async (userId: string) => {
      if (!activeConvId) return;
      await api.kickMember(activeConvId, userId);
      setMembers((prev) => prev.filter((m) => m.user_id !== userId));
    },
    [api, activeConvId],
  );

  const handleOpenInvite = useCallback(async () => {
    setInviteOpen(true);
    setLoadingUsers(true);
    try {
      const users = await api.listChatUsers();
      setAllUsers(users);
    } catch {
      setAllUsers([]);
    } finally {
      setLoadingUsers(false);
    }
  }, [api]);

  const handleInviteUsers = useCallback(
    async (invited: { user_id: string; display_name: string }[], revoked: string[]) => {
      if (!activeConvId) return;
      setInviteOpen(false);
      if (invited.length > 0) {
        await api.inviteMembers(activeConvId, invited);
      }
      for (const userId of revoked) {
        await api.revokeInvite(activeConvId, userId);
      }
      if (invited.length > 0 || revoked.length > 0) {
        // Refresh to get updated invite list
        const conv = await api.loadConversation(activeConvId);
        setPendingInvites(conv.invites || []);
      }
    },
    [api, activeConvId],
  );

  const handleSelectInvite = useCallback(
    (id: string) => {
      const conv = conversations.find((c) => c.conversation_id === id);
      setInviteResponseDialog({
        conversationId: id,
        title: conv?.title || "Room",
      });
    },
    [conversations],
  );

  const handleRespondInvite = useCallback(
    async (action: "accept" | "decline") => {
      if (!inviteResponseDialog) return;
      const { conversationId } = inviteResponseDialog;
      setInviteResponseDialog(null);
      await api.respondInvite(conversationId, action);
      refetchConversations();
      if (action === "accept") {
        loadConversation(conversationId);
      }
    },
    [api, inviteResponseDialog, refetchConversations, loadConversation],
  );

  const handleRename = useCallback(
    (id: string) => {
      const current =
        conversations.find((c) => c.conversation_id === id)?.title || "";
      setPromptDialog({
        title: "Rename",
        placeholder: "New name",
        defaultValue: current,
        submitLabel: "Save",
        onSubmit: async (title) => {
          setPromptDialog(null);
          await api.renameConversation(id, title);
          refetchConversations();
          if (id === activeConvId) setRoomTitle(title);
        },
      });
    },
    [api, activeConvId, conversations, refetchConversations],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      // If the chat has any tool-produced workspace files, warn the
      // user that deleting the chat will also wipe those files
      // permanently. Without the warning it's easy to lose generated
      // PDFs / images / etc. by accident — they live in
      // ``users/<u>/conversations/<conv>/<skill>/`` and the
      // ``chat.conversation.destroyed`` event triggers a full
      // ``rm -rf`` on the conv subtree (see ``SkillService._on_conversation_destroyed``).
      let hasFiles = false;
      let fileCount = 0;
      try {
        const conv = await api.loadConversation(id);
        for (const turn of conv.turns) {
          for (const att of turn.final_attachments) {
            if (att.workspace_path) {
              hasFiles = true;
              fileCount += 1;
            }
          }
        }
      } catch {
        // If load fails, fall through to the unconditional delete
        // path — better to delete than silently swallow the action.
      }
      if (hasFiles) {
        const ok = window.confirm(
          `This chat has ${fileCount} generated file${fileCount === 1 ? "" : "s"} ` +
            `(PDFs, images, etc.) attached to its messages. Deleting the chat ` +
            `will permanently remove ${fileCount === 1 ? "it" : "them"} from disk — ` +
            `download anything you want to keep first.\n\nDelete anyway?`,
        );
        if (!ok) return;
      }
      await api.deleteConversation(id);
      if (activeConvId === id) clearChat();
      refetchConversations();
    },
    [api, activeConvId, clearChat, refetchConversations],
  );

  // WebSocket event handlers
  const handleChatEvent = useCallback(
    (event: { event_type: string; data: Record<string, unknown> }) => {
      const data = event.data;
      const convId = data.conversation_id as string;

      switch (event.event_type) {
        case "chat.message.created":
          if (convId === activeConvId) {
            const isOwnMessage = data.author_id === user?.user_id;
            if (!isOwnMessage) {
              // Shared-room broadcast: another user posted a message
              // and Gilbert may have replied. We don't get structured
              // rounds in the broadcast payload, so build a single
              // flat turn with the user message + (optional) assistant
              // final content + attachments.
              const userText = (data.user_message as string) || "";
              const replyText = (data.content as string) || "";
              const replyAttachments =
                (data.attachments as FileAttachment[]) || [];
              if (userText || replyText || replyAttachments.length > 0) {
                setTurns((prev) => [
                  ...prev,
                  {
                    user_message: {
                      content: userText,
                      attachments: [],
                      author_id: data.author_id as string,
                      author_name: data.author_name as string,
                    },
                    rounds: [],
                    final_content: replyText,
                    final_attachments: replyAttachments,
                    incomplete: false,
                    streaming: false,
                  },
                ]);
              }
            }
            if ((data.ui_blocks as UIBlock[])?.length && !isOwnMessage) {
              setUiBlocks((prev) => [
                ...prev,
                ...(data.ui_blocks as UIBlock[]),
              ]);
            }
          }
          break;
        case "chat.member.joined":
          if (convId === activeConvId) {
            setMembers((prev) => [
              ...prev,
              {
                user_id: data.user_id as string,
                display_name: data.display_name as string,
                role: "member" as const,
              },
            ]);
          }
          refetchConversations();
          break;
        case "chat.member.left":
        case "chat.member.kicked":
          if (convId === activeConvId) {
            setMembers((prev) =>
              prev.filter((m) => m.user_id !== data.user_id),
            );
            if (data.user_id === user?.user_id) clearChat();
          }
          refetchConversations();
          break;
        case "chat.conversation.destroyed":
          if (convId === activeConvId) clearChat();
          refetchConversations();
          break;
        case "chat.conversation.renamed":
          if (convId === activeConvId) setRoomTitle(data.title as string);
          refetchConversations();
          break;
        case "chat.conversation.created":
          refetchConversations();
          break;
        case "chat.invite.created":
        case "chat.invite.declined":
          refetchConversations();
          break;
      }
    },
    [activeConvId, user?.user_id, clearChat, refetchConversations],
  );

  useEventBus("chat.message.created", handleChatEvent);
  useEventBus("chat.member.joined", handleChatEvent);
  useEventBus("chat.member.left", handleChatEvent);
  useEventBus("chat.member.kicked", handleChatEvent);
  useEventBus("chat.conversation.destroyed", handleChatEvent);
  useEventBus("chat.conversation.renamed", handleChatEvent);
  useEventBus("chat.conversation.created", handleChatEvent);
  useEventBus("chat.invite.created", handleChatEvent);
  useEventBus("chat.invite.declined", handleChatEvent);

  // Live streaming subscriptions. Mutate the in-flight turn (the last
  // entry in ``turns`` if it has ``streaming: true``) in place as
  // events arrive. When the chat.message.send RPC resolves, handleSend
  // replaces the streaming turn with the authoritative committed
  // shape from the server.
  //
  // Round model: text_delta appends to the LAST round's reasoning.
  // chat.stream.round_complete is the explicit "this round is done"
  // signal — when it fires, we set ``nextRoundPendingRef`` and the
  // next text_delta opens a fresh round. We don't infer round
  // boundaries from "does the last round have tools" because that
  // reasoning is racy against React state batching and breaks when
  // the AI emits a text-only round (no tools at all).
  //
  // tool.started adds a pending tool entry to the LAST round —
  // because tools execute AFTER round_complete fires for the round
  // that requested them, the last round when tool.started arrives
  // is exactly the round that wanted the tool. tool.completed
  // updates the entry by tool_call_id.
  const updateStreamingTurn = useCallback(
    (mutator: (turn: ChatTurn) => ChatTurn) => {
      setTurns((prev) => {
        const lastIdx = prev.length - 1;
        if (lastIdx < 0 || !prev[lastIdx].streaming) return prev;
        const next = [...prev];
        next[lastIdx] = mutator(next[lastIdx]);
        return next;
      });
    },
    [],
  );

  const handleTextDelta = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== activeConvIdRef.current) return;
      const chunk = event.data.text;
      if (typeof chunk !== "string" || !chunk) return;
      const startNewRound = nextRoundPendingRef.current;
      if (startNewRound) {
        nextRoundPendingRef.current = false;
      }
      updateStreamingTurn((turn) => {
        const rounds = [...turn.rounds];
        if (rounds.length === 0 || startNewRound) {
          rounds.push({ reasoning: "", tools: [] });
        }
        const lastIdx = rounds.length - 1;
        rounds[lastIdx] = {
          ...rounds[lastIdx],
          reasoning: rounds[lastIdx].reasoning + chunk,
        };
        return { ...turn, rounds };
      });
    },
    [updateStreamingTurn],
  );

  const handleToolStarted = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== activeConvIdRef.current) return;
      const toolName = String(event.data.tool_name || "");
      const toolCallId = String(event.data.tool_call_id || "");
      const args = (event.data.arguments as Record<string, unknown>) || {};
      updateStreamingTurn((turn) => {
        const rounds = [...turn.rounds];
        // If the round just ended (round_complete fired) but no text
        // followed, the next event is the tool that the model
        // requested. It belongs to the round that just finished, so
        // we attach it to the existing last round. Only create a new
        // round if there are no rounds at all yet (rare — a tool
        // call arriving before any text).
        if (rounds.length === 0) {
          rounds.push({ reasoning: "", tools: [] });
        }
        const lastIdx = rounds.length - 1;
        const newTool: ChatRoundTool = {
          tool_call_id: toolCallId,
          tool_name: toolName,
          arguments: args,
          status: "running",
          is_error: false,
        };
        rounds[lastIdx] = {
          ...rounds[lastIdx],
          tools: [...rounds[lastIdx].tools, newTool],
        };
        return { ...turn, rounds };
      });
    },
    [updateStreamingTurn],
  );

  const handleToolCompleted = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== activeConvIdRef.current) return;
      const toolCallId = String(event.data.tool_call_id || "");
      const isError = Boolean(event.data.is_error);
      const resultPreview =
        typeof event.data.result_preview === "string"
          ? event.data.result_preview
          : "";
      updateStreamingTurn((turn) => {
        const rounds = turn.rounds.map((round) => {
          const tools = round.tools.map((tool) =>
            tool.tool_call_id === toolCallId
              ? {
                  ...tool,
                  status: "done" as const,
                  is_error: isError,
                  result: resultPreview,
                }
              : tool,
          );
          return { ...round, tools };
        });
        return { ...turn, rounds };
      });
    },
    [updateStreamingTurn],
  );

  const handleRoundComplete = useCallback((event: GilbertEvent) => {
    if (event.data.conversation_id !== activeConvIdRef.current) return;
    // The current round is done. The next text_delta should start a
    // fresh round (to display alongside the now-finished one), not
    // extend the current one. tool events that arrive between now
    // and the next text_delta still belong to the round that just
    // ended — handleToolStarted attaches them to the existing last
    // round, which is correct.
    nextRoundPendingRef.current = true;
  }, []);

  const handleTurnComplete = useCallback((event: GilbertEvent) => {
    if (event.data.conversation_id !== activeConvIdRef.current) return;
    // Reset the round-transition flag so a subsequent send doesn't
    // inherit stale state. The RPC resolution is still the
    // authoritative commit point for the turn shape.
    nextRoundPendingRef.current = false;
  }, []);

  useEventBus("chat.stream.text_delta", handleTextDelta);
  useEventBus("chat.stream.round_complete", handleRoundComplete);
  useEventBus("chat.stream.turn_complete", handleTurnComplete);
  useEventBus("chat.tool.started", handleToolStarted);
  useEventBus("chat.tool.completed", handleToolCompleted);

  const sidebarProps = {
    conversations,
    activeId: activeConvId,
    currentUserId: user?.user_id,
    onSelect: loadConversation,
    onSelectInvite: handleSelectInvite,
    onJoinRoom: handleJoinRoom,
    onLeaveRoom: handleLeaveRoom,
    onRename: handleRename,
    onDelete: handleDelete,
  };

  const chatTitle = isShared && roomTitle
    ? roomTitle
    : activeConvId
      ? conversations.find((c) => c.conversation_id === activeConvId)?.title || "Chat"
      : "";

  return (
    <div className="flex h-full">
      {/* Desktop sidebar */}
      <div className="hidden md:flex w-64 shrink-0 border-r min-w-0 overflow-hidden">
        <ChatSidebarContent {...sidebarProps} />
      </div>

      {/* Mobile sidebar sheet */}
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetContent side="left" className="w-72 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>Conversations</SheetTitle>
          </SheetHeader>
          <ChatSidebarContent {...sidebarProps} />
        </SheetContent>
      </Sheet>

      {/* Main chat area */}
      <div
        ref={chatAreaRef}
        className="relative flex flex-1 flex-col min-w-0 overflow-hidden"
      >
        {/* Top bar */}
        <div className="flex items-center gap-2 shrink-0 border-b px-3 py-2">
          <Button
            variant="ghost"
            size="icon-sm"
            className="md:hidden shrink-0"
            onClick={() => setSidebarOpen(true)}
          >
            <MenuIcon className="size-4" />
          </Button>

          {/* Title */}
          <div className="flex-1 min-w-0 px-1">
            <h2 className="text-sm font-medium truncate">
              {chatTitle || "Chat"}
            </h2>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-1 shrink-0">
            {isShared && (
              <>
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={handleOpenInvite}
                      />
                    }
                  >
                    <UserPlusIcon className="size-4" />
                  </TooltipTrigger>
                  <TooltipContent>Invite users</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setMembersOpen(true)}
                      />
                    }
                  >
                    <UsersRoundIcon className="size-4 mr-1" />
                    <span className="text-xs">{members.length}</span>
                  </TooltipTrigger>
                  <TooltipContent>Members</TooltipContent>
                </Tooltip>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-muted-foreground"
                  onClick={() => activeConvId && handleLeaveRoom(activeConvId)}
                >
                  Leave
                </Button>
                <Separator orientation="vertical" className="h-5 mx-1" />
              </>
            )}

            {activeConvId && (
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => setWorkspaceOpen(!workspaceOpen)}
                    />
                  }
                >
                  <FolderOpenIcon className="size-4" />
                </TooltipTrigger>
                <TooltipContent>Workspace files</TooltipContent>
              </Tooltip>
            )}

            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => setSkillsOpen(true)}
                  />
                }
              >
                <SparklesIcon className="size-4" />
              </TooltipTrigger>
              <TooltipContent>Skills</TooltipContent>
            </Tooltip>

            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={handleNewChat}
                  />
                }
              >
                <PlusIcon className="size-4" />
              </TooltipTrigger>
              <TooltipContent>New chat</TooltipContent>
            </Tooltip>

            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={handleCreateRoom}
                  />
                }
              >
                <UsersRoundIcon className="size-4" />
              </TooltipTrigger>
              <TooltipContent>New room</TooltipContent>
            </Tooltip>
          </div>
        </div>

        {/* Messages or empty state */}
        {loadingConv ? (
          <div className="flex flex-1 flex-col items-center justify-center">
            <LoadingSpinner text="Loading conversation..." />
          </div>
        ) : !activeConvId && turns.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-4 text-muted-foreground p-8">
            <MessageSquareIcon className="size-12 opacity-20" />
            <div className="text-center space-y-1">
              <p className="text-sm font-medium">No conversation selected</p>
              <p className="text-xs">
                Pick a chat or room from the sidebar, or create a new one.
              </p>
            </div>
            <div className="flex gap-2 mt-2">
              <Button variant="outline" size="sm" onClick={handleNewChat}>
                <PlusIcon className="size-3.5 mr-1.5" />
                New Chat
              </Button>
              <Button variant="outline" size="sm" onClick={handleCreateRoom}>
                <UsersRoundIcon className="size-3.5 mr-1.5" />
                New Room
              </Button>
            </div>
          </div>
        ) : (
          <MessageList
            turns={turns}
            uiBlocks={uiBlocks}
            isShared={isShared}
            currentUserId={user?.user_id}
            onBlockSubmit={handleBlockSubmit}
          />
        )}

        {/* The sticky thinking-panel footer is gone — tool activity now
            renders inside each turn bubble's thinking card, in context. */}

        {/* Sticky input — only show when a conversation is active or turns exist */}
        {(activeConvId || turns.length > 0) && (
          <ChatInput
            onSend={handleSend}
            onStop={handleStop}
            sending={sending}
            placeholder={
              isShared
                ? "Mention 'Gilbert' for AI help..."
                : "Type a message..."
            }
            pendingAttachments={pendingAttachments}
            attachError={attachError}
            onAddFiles={addFiles}
            onRemoveAttachment={removeAttachment}
            onClearAttachments={clearAttachments}
            backends={modelsData?.backends}
            modelSelection={modelSelection}
            onModelChange={setModelSelection}
          />
        )}

        {/* Drag-and-drop overlay — rendered above the chat area when the
            user is dragging files in from the OS. */}
        {dragActive && (
          <div className="pointer-events-none absolute inset-0 z-40 flex items-center justify-center bg-background/70 backdrop-blur-sm">
            <div className="rounded-xl border-2 border-dashed border-primary bg-background px-6 py-5 text-center shadow-lg">
              <p className="text-sm font-medium">Drop files to attach</p>
              <p className="mt-1 text-xs text-muted-foreground">
                Images, PDFs, or text / code files
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Desktop member panel */}
      {isShared && members.length > 0 && (
        <div className="hidden lg:block w-52 shrink-0 border-l">
          <MemberPanelContent
            members={members}
            ownerId={ownerId}
            currentUserId={user?.user_id}
            onKick={handleKick}
          />
        </div>
      )}

      {/* Desktop workspace panel */}
      {workspaceOpen && activeConvId && (
        <div className="hidden md:block w-64 shrink-0 border-l overflow-hidden">
          <WorkspacePanelContent conversationId={activeConvId} />
        </div>
      )}

      {/* Mobile member panel sheet */}
      <Sheet open={membersOpen} onOpenChange={setMembersOpen}>
        <SheetContent side="right" className="w-64 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>Members</SheetTitle>
          </SheetHeader>
          <MemberPanelContent
            members={members}
            ownerId={ownerId}
            currentUserId={user?.user_id}
            onKick={handleKick}
          />
        </SheetContent>
      </Sheet>

      {/* Mobile workspace panel sheet */}
      <Sheet
        open={workspaceOpen && !!activeConvId && typeof window !== "undefined" && window.innerWidth < 768}
        onOpenChange={setWorkspaceOpen}
      >
        <SheetContent side="right" className="w-72 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>Workspace Files</SheetTitle>
          </SheetHeader>
          {activeConvId && (
            <WorkspacePanelContent conversationId={activeConvId} />
          )}
        </SheetContent>
      </Sheet>

      <PromptDialog
        open={!!promptDialog}
        title={promptDialog?.title || ""}
        placeholder={promptDialog?.placeholder}
        defaultValue={promptDialog?.defaultValue}
        submitLabel={promptDialog?.submitLabel}
        onSubmit={(v) => promptDialog?.onSubmit(v)}
        onCancel={() => setPromptDialog(null)}
      />

      <InviteModal
        open={inviteOpen}
        users={allUsers}
        existingMemberIds={members.map((m) => m.user_id)}
        pendingInviteIds={pendingInvites.map((i) => i.user_id)}
        currentUserId={user?.user_id}
        loading={loadingUsers}
        onInvite={handleInviteUsers}
        onCancel={() => setInviteOpen(false)}
      />

      <SkillsModal
        open={skillsOpen}
        conversationId={activeConvId}
        onClose={() => setSkillsOpen(false)}
      />

      <Dialog
        open={!!inviteResponseDialog}
        onOpenChange={(o) => !o && setInviteResponseDialog(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Room Invitation</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            You've been invited to join{" "}
            <span className="font-medium text-foreground">
              {inviteResponseDialog?.title}
            </span>
            . Would you like to join?
          </p>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setInviteResponseDialog(null)}
            >
              Cancel
            </Button>
            <Button
              variant="outline"
              className="text-destructive"
              onClick={() => handleRespondInvite("decline")}
            >
              Decline
            </Button>
            <Button onClick={() => handleRespondInvite("accept")}>
              Join
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
