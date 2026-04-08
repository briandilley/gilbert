import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/hooks/useAuth";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { ChatMessage } from "@/types/chat";
import type { UIBlock } from "@/types/ui";
import { ChatSidebarContent } from "./ChatSidebar";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { MemberPanelContent } from "./MemberPanel";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  MenuIcon,
  MessageSquareIcon,
  PlusIcon,
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
  const [messages, setMessages] = useState<ChatMessage[]>([]);
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
  const [promptDialog, setPromptDialog] = useState<{
    title: string;
    placeholder?: string;
    defaultValue?: string;
    submitLabel?: string;
    onSubmit: (value: string) => void;
  } | null>(null);

  const { data: conversations = [], refetch: refetchConversations } = useQuery({
    queryKey: ["conversations"],
    queryFn: api.listConversations,
    enabled: connected,
  });

  const loadConversation = useCallback(
    async (id: string) => {
      setLoadingConv(true);
      try {
        const conv = await api.loadConversation(id);
        setActiveConvId(id);
        setMessages(conv.messages);
        setUiBlocks(conv.ui_blocks || []);
        setIsShared(conv.shared);
        setMembers(
          (conv.members || []).map((m) => ({
            ...m,
            role: m.role as "owner" | "member" | undefined,
          })),
        );
        setOwnerId(conv.owner_id || "");
        setRoomTitle(conv.title);
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
    async (message: string) => {
      setMessages((prev) => [...prev, { role: "user", content: message }]);
      setSending(true);

      try {
        const resp = await api.sendMessage(message, activeConvId);
        setActiveConvId(resp.conversation_id);

        if (resp.response) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: resp.response },
          ]);
        }

        if (resp.ui_blocks?.length) {
          setUiBlocks((prev) => [...prev, ...resp.ui_blocks]);
        }

        refetchConversations();
      } catch {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: "Sorry, something went wrong. Please try again.",
          },
        ]);
      } finally {
        setSending(false);
      }
    },
    [api, activeConvId, refetchConversations],
  );

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
        if (resp.response) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: resp.response },
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
    setMessages([]);
    setUiBlocks([]);
    setIsShared(false);
    setMembers([]);
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
          setMessages([]);
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
            if (data.user_message && !isOwnMessage) {
              setMessages((prev) => [
                ...prev,
                {
                  role: "user",
                  content: data.user_message as string,
                  author_id: data.author_id as string,
                  author_name: data.author_name as string,
                },
              ]);
            }
            if (data.content && !isOwnMessage) {
              setMessages((prev) => [
                ...prev,
                { role: "assistant", content: data.content as string },
              ]);
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

  const sidebarProps = {
    conversations,
    activeId: activeConvId,
    currentUserId: user?.user_id,
    onSelect: loadConversation,
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
    <div className="flex h-[calc(100dvh-3.5rem)]">
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
      <div className="flex flex-1 flex-col min-w-0">
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
        ) : !activeConvId && messages.length === 0 ? (
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
            messages={messages}
            uiBlocks={uiBlocks}
            isShared={isShared}
            currentUserId={user?.user_id}
            onBlockSubmit={handleBlockSubmit}
          />
        )}

        {/* Thinking indicator */}
        {sending && (
          <div className="shrink-0 px-4 pb-2">
            <div className="max-w-3xl mx-auto">
              <LoadingSpinner text="Thinking..." />
            </div>
          </div>
        )}

        {/* Sticky input — only show when a conversation is active or messages exist */}
        {(activeConvId || messages.length > 0) && (
          <ChatInput
            onSend={handleSend}
            disabled={sending}
            placeholder={
              isShared
                ? "Mention 'Gilbert' for AI help..."
                : "Type a message..."
            }
          />
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

      <PromptDialog
        open={!!promptDialog}
        title={promptDialog?.title || ""}
        placeholder={promptDialog?.placeholder}
        defaultValue={promptDialog?.defaultValue}
        submitLabel={promptDialog?.submitLabel}
        onSubmit={(v) => promptDialog?.onSubmit(v)}
        onCancel={() => setPromptDialog(null)}
      />
    </div>
  );
}
