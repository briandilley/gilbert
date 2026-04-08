/**
 * WebSocket RPC API hook — typed functions for all Gilbert operations.
 *
 * Components call `const api = useWsApi()` then `api.listConversations()`, etc.
 * Each function sends a typed WS frame and returns a Promise resolved when
 * the server responds with a matching `.result` frame.
 */

import { useCallback, useMemo } from "react";
import { useWebSocket } from "./useWebSocket";
import type { ConversationSummary, ConversationDetail, ChatResponse, ConversationMember } from "@/types/chat";
import type { Role, ToolPermission, AIProfile, UserRoleAssignment, CollectionACL } from "@/types/roles";
import type { DocumentNode, SearchResult } from "@/types/documents";
import type { DashboardCard } from "@/types/dashboard";
import type { ServiceInfo } from "@/types/system";
import type { CollectionGroup, CollectionData, EntityData } from "@/types/entities";
import type { InboxStats, InboxMessage, MessageDetail, PendingReply } from "@/types/inbox";
import type { UIBlock } from "@/types/ui";

export function useWsApi() {
  const { rpc } = useWebSocket();

  return useMemo(() => ({
    // ── Chat ──────────────────────────────────────────────────────

    listConversations: () =>
      rpc<{ conversations: ConversationSummary[] }>({ type: "chat.conversation.list" })
        .then((r) => r.conversations),

    loadConversation: (conversationId: string) =>
      rpc<ConversationDetail>({ type: "chat.history.load", conversation_id: conversationId }),

    sendMessage: (message: string, conversationId: string | null) =>
      rpc<ChatResponse>({ type: "chat.message.send", message, conversation_id: conversationId }),

    submitForm: (conversationId: string, blockId: string, values: Record<string, unknown>) =>
      rpc<ChatResponse>({ type: "chat.form.submit", conversation_id: conversationId, block_id: blockId, values }),

    renameConversation: (conversationId: string, title: string) =>
      rpc<{ status: string; title: string }>({ type: "chat.conversation.rename", conversation_id: conversationId, title }),

    deleteConversation: (conversationId: string) =>
      rpc<{ status: string }>({ type: "chat.conversation.delete", conversation_id: conversationId }),

    createRoom: (title: string, visibility: "public" | "invite" = "public") =>
      rpc<{ conversation_id: string; title: string; members: ConversationMember[] }>(
        { type: "chat.room.create", title, visibility },
      ),

    joinRoom: (conversationId: string) =>
      rpc<{ status: string }>({ type: "chat.room.join", conversation_id: conversationId }),

    leaveRoom: (conversationId: string) =>
      rpc<{ status: string }>({ type: "chat.room.leave", conversation_id: conversationId }),

    kickMember: (conversationId: string, userId: string) =>
      rpc<{ status: string }>({ type: "chat.room.kick", conversation_id: conversationId, user_id: userId }),

    inviteMember: (conversationId: string, userId: string, displayName: string) =>
      rpc<{ status: string }>({
        type: "chat.room.invite",
        conversation_id: conversationId,
        user_id: userId,
        display_name: displayName,
      }),

    // ── Roles ─────────────────────────────────────────────────────

    listRoles: () =>
      rpc<{ roles: Role[] }>({ type: "roles.role.list" }),

    createRole: (name: string, level: number, description: string) =>
      rpc<{ status: string }>({ type: "roles.role.create", name, level, description }),

    updateRole: (name: string, level: number, description: string) =>
      rpc<{ status: string }>({ type: "roles.role.update", name, level, description }),

    deleteRole: (name: string) =>
      rpc<{ status: string }>({ type: "roles.role.delete", name }),

    listToolPermissions: () =>
      rpc<{ tools: ToolPermission[]; role_names: string[] }>({ type: "roles.tool.list" }),

    setToolRole: (toolName: string, role: string) =>
      rpc<{ status: string }>({ type: "roles.tool.set", tool_name: toolName, role }),

    clearToolRole: (toolName: string) =>
      rpc<{ status: string }>({ type: "roles.tool.clear", tool_name: toolName }),

    listProfiles: () =>
      rpc<{ profiles: AIProfile[]; declared_calls: string[]; profile_names: string[]; all_tool_names: string[] }>(
        { type: "roles.profile.list" },
      ),

    saveProfile: (profile: { name: string; description: string; tool_mode: string; tools: string[]; tool_roles: Record<string, string> }) =>
      rpc<{ status: string }>({ type: "roles.profile.save", ...profile }),

    deleteProfile: (name: string) =>
      rpc<{ status: string }>({ type: "roles.profile.delete", name }),

    assignProfile: (aiCall: string, profileName: string) =>
      rpc<{ status: string }>({ type: "roles.profile.assign", ai_call: aiCall, profile_name: profileName }),

    listUserRoles: () =>
      rpc<{ users: UserRoleAssignment[]; role_names: string[] }>({ type: "roles.user.list" }),

    setUserRoles: (userId: string, roles: string[]) =>
      rpc<{ status: string }>({ type: "roles.user.set", user_id: userId, roles }),

    listCollectionACLs: () =>
      rpc<{ collections: CollectionACL[]; role_names: string[] }>({ type: "roles.collection.list" }),

    setCollectionACL: (collection: string, readRole: string, writeRole: string) =>
      rpc<{ status: string }>({ type: "roles.collection.set", collection, read_role: readRole, write_role: writeRole }),

    clearCollectionACL: (collection: string) =>
      rpc<{ status: string }>({ type: "roles.collection.clear", collection }),

    listEventVisibility: () =>
      rpc<{ rules: { event_prefix: string; min_role: string; source: string }[]; role_names: string[] }>(
        { type: "roles.event_visibility.list" },
      ),

    setEventVisibility: (eventPrefix: string, minRole: string) =>
      rpc<{ status: string }>({ type: "roles.event_visibility.set", event_prefix: eventPrefix, min_role: minRole }),

    clearEventVisibility: (eventPrefix: string) =>
      rpc<{ status: string }>({ type: "roles.event_visibility.clear", event_prefix: eventPrefix }),

    listRpcPermissions: () =>
      rpc<{ rules: { frame_prefix: string; min_role: string; source: string }[]; role_names: string[] }>(
        { type: "roles.rpc_permissions.list" },
      ),

    setRpcPermission: (framePrefix: string, minRole: string) =>
      rpc<{ status: string }>({ type: "roles.rpc_permissions.set", frame_prefix: framePrefix, min_role: minRole }),

    clearRpcPermission: (framePrefix: string) =>
      rpc<{ status: string }>({ type: "roles.rpc_permissions.clear", frame_prefix: framePrefix }),

    // ── Inbox ─────────────────────────────────────────────────────

    inboxStats: () =>
      rpc<InboxStats>({ type: "inbox.stats.get" }),

    listMessages: (params?: { sender?: string; subject?: string; limit?: number }) =>
      rpc<{ messages: InboxMessage[]; total: number }>({ type: "inbox.message.list", ...params })
        .then((r) => r.messages),

    getMessage: (messageId: string) =>
      rpc<MessageDetail>({ type: "inbox.message.get", message_id: messageId }),

    getThread: (threadId: string) =>
      rpc<{ messages: MessageDetail[] }>({ type: "inbox.thread.get", thread_id: threadId })
        .then((r) => r.messages),

    listPending: () =>
      rpc<{ pending: PendingReply[] }>({ type: "inbox.pending.list" })
        .then((r) => r.pending),

    cancelPending: (replyId: string) =>
      rpc<{ status: string }>({ type: "inbox.pending.cancel", reply_id: replyId }),

    // ── Documents ─────────────────────────────────────────────────

    listDocumentSources: () =>
      rpc<{ sources: { source_id: string; source_name: string }[] }>({ type: "documents.sources.list" })
        .then((r) => r.sources),

    browseDocuments: (sourceId: string, path?: string) =>
      rpc<{ source_id: string; path: string; children: DocumentNode[] }>({
        type: "documents.browse", source_id: sourceId, path: path || "",
      }).then((r) => r.children),

    searchDocuments: (query: string, sourceId?: string) =>
      rpc<{ results: SearchResult[]; query: string }>({ type: "documents.search", query, source_id: sourceId })
        .then((r) => r.results),

    // ── Dashboard ─────────────────────────────────────────────────

    getDashboard: () =>
      rpc<{ cards: DashboardCard[] }>({ type: "dashboard.get" }),

    // ── System ────────────────────────────────────────────────────

    listServices: () =>
      rpc<{ services: ServiceInfo[] }>({ type: "system.services.list" }),

    // ── Entities ──────────────────────────────────────────────────

    listCollections: () =>
      rpc<{ groups: CollectionGroup[] }>({ type: "entities.collection.list" }),

    queryCollection: (collection: string, params?: { page?: number; sort?: string; order?: string }) =>
      rpc<CollectionData>({ type: "entities.collection.query", collection, ...params }),

    getEntity: (collection: string, entityId: string) =>
      rpc<EntityData>({ type: "entities.entity.get", collection, entity_id: entityId }),

    // ── Screens ───────────────────────────────────────────────────

    listScreens: () =>
      rpc<{ screens: { name: string; key: string; connected_at: string }[] }>({ type: "screens.list" }),

  }), [rpc]);
}
