/**
 * WebSocket RPC API hook — typed functions for all Gilbert operations.
 *
 * Components call `const api = useWsApi()` then `api.listConversations()`, etc.
 * Each function sends a typed WS frame and returns a Promise resolved when
 * the server responds with a matching `.result` frame.
 */

import { useCallback, useMemo } from "react";
import { useWebSocket } from "./useWebSocket";
import type {
  ConversationSummary,
  ConversationDetail,
  ChatResponse,
  ConversationMember,
  FileAttachment,
  ModelsListResult,
} from "@/types/chat";
import type { Role, ToolPermission, AIProfile, UserRoleAssignment, CollectionACL } from "@/types/roles";
import type { DocumentNode, SearchResult } from "@/types/documents";
import type { DashboardResponse } from "@/types/dashboard";
import type { ServiceInfo } from "@/types/system";
import type { CollectionGroup, CollectionData, EntityData } from "@/types/entities";
import type {
  InboxStats,
  InboxMessage,
  MessageDetail,
  InboxMailbox,
  OutboxEntry,
  OutboxStatus,
  EmailBackendInfo,
} from "@/types/inbox";
import type { UIBlock } from "@/types/ui";
import type { SkillInfo } from "@/types/skills";
import type {
  ConfigActionInvokeResponse,
  ConfigDescribeResponse,
  ConfigSectionResponse,
  ConfigSetResult,
} from "@/types/config";
import type { Job } from "@/types/scheduler";
import type { SlashCommand } from "@/types/slash";
import type { InstalledPlugin, InstallPluginResponse } from "@/types/plugins";
import type {
  McpResourceContent,
  McpResourceSpec,
  McpServer,
  McpServerClient,
  McpServerClientDraft,
  McpServerDraft,
  McpToolSpec,
} from "@/types/mcp";
import type { WorkspaceFile } from "@/types/workspace";
import type {
  UsageAggregate,
  UsageDimensions,
  UsageQueryPayload,
} from "@/types/usage";
import type {
  Proposal,
  ProposalsListResult,
  ProposalsListCyclesResult,
} from "@/types/proposals";
import type {
  Notification,
  NotificationListResult,
  NotificationUrgency,
} from "@/types/notifications";
import type {
  Goal,
  AgentRun,
  GoalCreatePayload,
  GoalUpdatePayload,
} from "@/types/agent";

export function useWsApi() {
  const { rpc, rpcWithRef } = useWebSocket();

  return useMemo(() => ({
    // ── Chat ──────────────────────────────────────────────────────

    listConversations: () =>
      rpc<{ conversations: ConversationSummary[] }>({ type: "chat.conversation.list" })
        .then((r) => r.conversations),

    loadConversation: (conversationId: string) =>
      rpc<ConversationDetail>({ type: "chat.history.load", conversation_id: conversationId }),

    createConversation: (title: string) =>
      rpc<{ conversation_id: string; title: string }>({ type: "chat.conversation.create", title }),

    /**
     * Upload a file to a conversation's chat-uploads workspace via
     * the HTTP endpoint, returning a reference-mode FileAttachment.
     *
     * Uses ``XMLHttpRequest`` rather than ``fetch`` because fetch
     * doesn't surface upload progress yet — XHR gives us
     * ``upload.onprogress`` events so the UI can show a progress
     * bar for big files.
     *
     * This bypasses the WebSocket entirely — the bytes stream
     * directly to disk under
     * ``.gilbert/skill-workspaces/users/<u>/conversations/<c>/chat-uploads/<name>``
     * and the chat message only carries the workspace coordinates.
     * That's how we support 1 GB uploads without blowing past the
     * WS frame limit or bloating the conversation entity.
     */
    uploadChatFile: (
      conversationId: string,
      file: File,
      onProgress?: (loaded: number, total: number) => void,
    ): Promise<FileAttachment> => {
      return new Promise<FileAttachment>((resolve, reject) => {
        const form = new FormData();
        form.append("conversation_id", conversationId);
        form.append("file", file);

        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/chat/upload");
        xhr.responseType = "json";
        xhr.withCredentials = true; // carry the session cookie
        if (onProgress) {
          xhr.upload.onprogress = (ev) => {
            if (ev.lengthComputable) {
              onProgress(ev.loaded, ev.total);
            }
          };
        }
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve(xhr.response as FileAttachment);
          } else {
            const detail =
              (xhr.response && (xhr.response as { detail?: string }).detail) ||
              `HTTP ${xhr.status}`;
            reject(new Error(detail));
          }
        };
        xhr.onerror = () => reject(new Error("Network error during upload"));
        xhr.onabort = () => reject(new Error("Upload aborted"));
        xhr.send(form);
      });
    },

    sendMessage: (
      message: string,
      conversationId: string | null,
      attachments: FileAttachment[] = [],
      modelOpts?: { model?: string; backend?: string },
    ) =>
      rpc<ChatResponse>({
        type: "chat.message.send",
        message,
        conversation_id: conversationId,
        attachments,
        ...(modelOpts?.model ? { model: modelOpts.model } : {}),
        ...(modelOpts?.backend ? { backend: modelOpts.backend } : {}),
      }),

    sendMessageWithRef: (
      message: string,
      conversationId: string | null,
      attachments: FileAttachment[] = [],
      modelOpts?: { model?: string; backend?: string },
    ) =>
      rpcWithRef<ChatResponse>({
        type: "chat.message.send",
        message,
        conversation_id: conversationId,
        attachments,
        ...(modelOpts?.model ? { model: modelOpts.model } : {}),
        ...(modelOpts?.backend ? { backend: modelOpts.backend } : {}),
      }),

    /**
     * Interrupt an in-flight ``chat.message.send`` by the ``ref`` it
     * was sent under. The backend cancels the running asyncio task,
     * ``AIService.chat()`` catches the ``CancelledError``, persists
     * partial state, and the original ``sendMessage`` promise
     * resolves with ``interrupted=true``. Only the originator of the
     * turn can cancel it — cross-user cancels return a 403.
     */
    cancelMessage: (ref: string) =>
      rpc<{ cancelled: boolean; reason?: string }>({
        type: "chat.message.cancel",
        ref,
      }),

    listModels: () =>
      rpc<ModelsListResult>({ type: "chat.models.list" }),

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

    inviteMembers: (conversationId: string, users: { user_id: string; display_name: string }[]) =>
      rpc<{ status: string; invited: { user_id: string; display_name: string }[] }>({
        type: "chat.room.invite",
        conversation_id: conversationId,
        user_ids: users,
      }),

    revokeInvite: (conversationId: string, userId: string) =>
      rpc<{ status: string }>({
        type: "chat.room.invite_revoke",
        conversation_id: conversationId,
        user_id: userId,
      }),

    respondInvite: (conversationId: string, action: "accept" | "decline") =>
      rpc<{ status: string; action: string }>({
        type: "chat.room.invite_respond",
        conversation_id: conversationId,
        action,
      }),

    listChatUsers: () =>
      rpc<{ users: { user_id: string; display_name: string }[] }>({ type: "chat.user.list" })
        .then((r) => r.users),

    listSlashCommands: () =>
      rpc<{ commands: SlashCommand[] }>({ type: "slash.commands.list" })
        .then((r) => r.commands),

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

    saveProfile: (profile: { name: string; description: string; tool_mode: string; tools: string[]; tool_roles: Record<string, string>; backend?: string; model?: string }) =>
      rpc<{ status: string }>({ type: "roles.profile.save", ...profile }),

    deleteProfile: (name: string) =>
      rpc<{ status: string }>({ type: "roles.profile.delete", name }),

    assignProfile: (aiCall: string, profileName: string) =>
      rpc<{ status: string }>({ type: "roles.profile.assign", ai_call: aiCall, profile_name: profileName }),

    listUserRoles: () =>
      rpc<{ users: UserRoleAssignment[]; role_names: string[]; allow_user_creation: boolean }>({ type: "roles.user.list" }),

    setUserRoles: (userId: string, roles: string[]) =>
      rpc<{ status: string }>({ type: "roles.user.set", user_id: userId, roles }),

    createUser: (params: { username: string; password: string; email?: string; display_name?: string }) =>
      rpc<{ status: string; user: UserRoleAssignment }>({ type: "users.user.create", ...params }),

    deleteUser: (userId: string) =>
      rpc<{ status: string }>({ type: "users.user.delete", user_id: userId }),

    resetUserPassword: (userId: string, password: string) =>
      rpc<{ status: string }>({ type: "users.user.reset_password", user_id: userId, password }),

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

    // ── Inbox: messages / stats ───────────────────────────────────

    inboxStats: (mailboxId?: string) =>
      rpc<InboxStats>({
        type: "inbox.stats.get",
        ...(mailboxId ? { mailbox_id: mailboxId } : {}),
      }),

    listMessages: (params?: {
      mailbox_id?: string; sender?: string; subject?: string; limit?: number;
    }) =>
      rpc<{ messages: InboxMessage[]; total: number }>({
        type: "inbox.message.list", ...params,
      }).then((r) => r.messages),

    getMessage: (messageId: string) =>
      rpc<MessageDetail>({ type: "inbox.message.get", message_id: messageId }),

    getThread: (threadId: string, mailboxId?: string) =>
      rpc<{ messages: MessageDetail[] }>({
        type: "inbox.thread.get",
        thread_id: threadId,
        ...(mailboxId ? { mailbox_id: mailboxId } : {}),
      }).then((r) => r.messages),

    // ── Inbox: outbox ─────────────────────────────────────────────

    listOutbox: (params?: { mailbox_id?: string; status?: OutboxStatus }) =>
      rpc<{ entries: OutboxEntry[] }>({
        type: "inbox.outbox.list", ...params,
      }).then((r) => r.entries),

    cancelOutbox: (outboxId: string) =>
      rpc<{ status: string }>({ type: "inbox.outbox.cancel", outbox_id: outboxId }),

    // ── Inbox: mailboxes ──────────────────────────────────────────

    listMailboxes: () =>
      rpc<{ mailboxes: InboxMailbox[] }>({ type: "inbox.mailboxes.list" })
        .then((r) => r.mailboxes),

    getMailbox: (mailboxId: string) =>
      rpc<{ mailbox: InboxMailbox }>({ type: "inbox.mailboxes.get", mailbox_id: mailboxId })
        .then((r) => r.mailbox),

    createMailbox: (mailbox: {
      name: string;
      email_address: string;
      backend_name: string;
      backend_config: Record<string, unknown>;
      poll_enabled?: boolean;
      poll_interval_sec?: number;
    }) =>
      rpc<{ mailbox: InboxMailbox }>({ type: "inbox.mailboxes.create", ...mailbox })
        .then((r) => r.mailbox),

    updateMailbox: (mailboxId: string, updates: Record<string, unknown>) =>
      rpc<{ mailbox: InboxMailbox }>({
        type: "inbox.mailboxes.update", mailbox_id: mailboxId, updates,
      }).then((r) => r.mailbox),

    deleteMailbox: (mailboxId: string) =>
      rpc<{ status: string }>({ type: "inbox.mailboxes.delete", mailbox_id: mailboxId }),

    testMailboxConnection: (mailboxId: string) =>
      rpc<{ ok: boolean; error: string }>({
        type: "inbox.mailboxes.test_connection", mailbox_id: mailboxId,
      }),

    shareMailboxUser: (mailboxId: string, userId: string) =>
      rpc<{ mailbox: InboxMailbox }>({
        type: "inbox.mailboxes.share_user", mailbox_id: mailboxId, user_id: userId,
      }).then((r) => r.mailbox),

    unshareMailboxUser: (mailboxId: string, userId: string) =>
      rpc<{ mailbox: InboxMailbox }>({
        type: "inbox.mailboxes.unshare_user", mailbox_id: mailboxId, user_id: userId,
      }).then((r) => r.mailbox),

    shareMailboxRole: (mailboxId: string, role: string) =>
      rpc<{ mailbox: InboxMailbox }>({
        type: "inbox.mailboxes.share_role", mailbox_id: mailboxId, role,
      }).then((r) => r.mailbox),

    unshareMailboxRole: (mailboxId: string, role: string) =>
      rpc<{ mailbox: InboxMailbox }>({
        type: "inbox.mailboxes.unshare_role", mailbox_id: mailboxId, role,
      }).then((r) => r.mailbox),

    listEmailBackends: () =>
      rpc<{ backends: EmailBackendInfo[] }>({ type: "inbox.backends.list" })
        .then((r) => r.backends),

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
      rpc<DashboardResponse>({ type: "dashboard.get" }),

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

    // ── Skills ────────────────────────────────────────────────────

    listSkills: () =>
      rpc<{ skills: SkillInfo[] }>({ type: "skills.list" })
        .then((r) => r.skills),

    getConversationSkills: (conversationId: string) =>
      rpc<{ active_skills: string[] }>({ type: "skills.conversation.active", conversation_id: conversationId })
        .then((r) => r.active_skills),

    toggleConversationSkill: (conversationId: string, skill: string, enabled: boolean) =>
      rpc<{ skill: string; enabled: boolean; active_skills: string[] }>({
        type: "skills.conversation.toggle",
        conversation_id: conversationId,
        skill,
        enabled,
      }),

    browseSkillWorkspace: (skillName: string) =>
      rpc<{ skill_name: string; files: { path: string; size: number; modified: string }[] }>({
        type: "skills.workspace.browse",
        skill_name: skillName,
      }),

    downloadSkillWorkspaceFile: (
      skillName: string,
      path: string,
      conversationId?: string,
    ) =>
      rpc<{
        filename: string;
        size: number;
        content_base64: string;
        media_type?: string;
      }>({
        type: "skills.workspace.download",
        skill_name: skillName,
        path,
        // The backend resolver tries the per-conversation workspace
        // first when this is set, then falls back to the legacy
        // per-(user, skill) path. Old attachments persisted before
        // per-conversation workspaces leave conversation_id undefined
        // and resolve via the legacy fallback.
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }),

    // ── Config ─────────────────────────────────────────────────────

    describeConfig: () =>
      rpc<ConfigDescribeResponse>({ type: "config.describe.list" }),

    getConfigSection: (namespace: string) =>
      rpc<ConfigSectionResponse>({ type: "config.section.get", namespace }),

    setConfigSection: (namespace: string, values: Record<string, unknown>) =>
      rpc<ConfigSetResult>({ type: "config.section.set", namespace, values }),

    resetConfigSection: (namespace: string) =>
      rpc<{ status: string }>({ type: "config.section.reset", namespace }),

    invokeConfigAction: (
      namespace: string,
      key: string,
      payload: Record<string, unknown> = {},
    ) =>
      rpc<ConfigActionInvokeResponse>({
        type: "config.action.invoke",
        namespace,
        key,
        payload,
      }),

    authorPrompt: (args: {
      namespace: string;
      key: string;
      currentText: string;
      instruction: string;
      aiProfile?: string;
    }) =>
      rpc<{
        namespace: string;
        key: string;
        new_text: string;
        profile_used: string;
      }>({
        type: "config.prompt.author",
        namespace: args.namespace,
        key: args.key,
        current_text: args.currentText,
        instruction: args.instruction,
        ai_profile: args.aiProfile ?? "",
      }),

    listAiProfiles: () =>
      rpc<{ profiles: { name: string; description: string }[] }>({
        type: "ai.profiles.list",
      }).then((r) => r.profiles),

    // ── Plugins ───────────────────────────────────────────────────

    listPlugins: () =>
      rpc<{ plugins: InstalledPlugin[] }>({ type: "plugins.list" })
        .then((r) => r.plugins),

    installPlugin: (url: string, force = false) =>
      rpc<InstallPluginResponse>({ type: "plugins.install", url, force }),

    uninstallPlugin: (name: string) =>
      rpc<{ status: string; name: string }>({ type: "plugins.uninstall", name }),

    restartHost: () =>
      rpc<{ status: string; pending_plugins: string[] }>({
        type: "plugins.restart_host",
      }),

    // ── Usage reporting ───────────────────────────────────────────

    queryUsage: (payload: UsageQueryPayload) =>
      rpc<{ rows: UsageAggregate[] }>({
        type: "usage.query",
        payload,
      }).then((r) => r.rows),

    listUsageDimensions: () =>
      rpc<UsageDimensions>({ type: "usage.dimensions" }),

    // ── Scheduler ─────────────────────────────────────────────────

    listJobs: (includeSystem = true) =>
      rpc<{ jobs: Job[] }>({ type: "scheduler.job.list", include_system: includeSystem })
        .then((r) => r.jobs),

    getJob: (name: string) =>
      rpc<{ job: Job }>({ type: "scheduler.job.get", name })
        .then((r) => r.job),

    enableJob: (name: string) =>
      rpc<{ status: string; name: string }>({ type: "scheduler.job.enable", name }),

    disableJob: (name: string) =>
      rpc<{ status: string; name: string }>({ type: "scheduler.job.disable", name }),

    removeJob: (name: string) =>
      rpc<{ status: string; name: string }>({ type: "scheduler.job.remove", name }),

    runJobNow: (name: string) =>
      rpc<{ status: string; name: string }>({ type: "scheduler.job.run_now", name }),

    // ── MCP (Model Context Protocol) ──────────────────────────────

    listMcpServers: () =>
      rpc<{ servers: McpServer[] }>({ type: "mcp.servers.list" })
        .then((r) => r.servers),

    createMcpServer: (draft: McpServerDraft) =>
      rpc<{ server: McpServer }>({ type: "mcp.servers.create", server: draft })
        .then((r) => r.server),

    updateMcpServer: (draft: McpServerDraft) =>
      rpc<{ server: McpServer }>({ type: "mcp.servers.update", server: draft })
        .then((r) => r.server),

    deleteMcpServer: (server_id: string) =>
      rpc<{ server_id: string }>({ type: "mcp.servers.delete", server_id }),

    startMcpServer: (server_id: string) =>
      rpc<{ server_id: string; connected: boolean; last_error: string | null }>({
        type: "mcp.servers.start",
        server_id,
      }),

    stopMcpServer: (server_id: string) =>
      rpc<{ server_id: string }>({ type: "mcp.servers.stop", server_id }),

    testMcpServer: (draft: McpServerDraft) =>
      rpc<{ tools: McpToolSpec[] }>({ type: "mcp.servers.test", server: draft })
        .then((r) => r.tools),

    listMcpServerTools: (server_id: string) =>
      rpc<{
        server_id: string;
        connected: boolean;
        last_error: string | null;
        tools: McpToolSpec[];
      }>({ type: "mcp.servers.tools", server_id }),

    startMcpOAuth: (server_id: string) =>
      rpc<{
        server_id: string;
        authorization_url: string;
        state: string;
      }>({ type: "mcp.servers.oauth_start", server_id }),

    cancelMcpOAuth: (server_id: string) =>
      rpc<{ server_id: string }>({ type: "mcp.servers.oauth_cancel", server_id }),

    listMcpResources: (server_id: string) =>
      rpc<{ server_id: string; resources: McpResourceSpec[] }>({
        type: "mcp.servers.resources.list",
        server_id,
      }).then((r) => r.resources),

    readMcpResource: (server_id: string, uri: string) =>
      rpc<{
        server_id: string;
        uri: string;
        contents: McpResourceContent[];
      }>({ type: "mcp.servers.resources.read", server_id, uri }).then(
        (r) => r.contents,
      ),

    listMcpPrompts: (server_id: string) =>
      rpc<{
        server_id: string;
        prompts: {
          name: string;
          title: string;
          description: string;
          arguments: {
            name: string;
            description: string;
            required: boolean;
          }[];
        }[];
      }>({ type: "mcp.servers.prompts.list", server_id }).then(
        (r) => r.prompts,
      ),

    renderMcpPrompt: (
      server_id: string,
      name: string,
      args: Record<string, string>,
    ) =>
      rpc<{
        server_id: string;
        name: string;
        description: string;
        messages: {
          role: "user" | "assistant" | "system";
          content: {
            type: string;
            text: string;
            mime_type: string;
            uri: string;
            data: string;
          };
        }[];
      }>({
        type: "mcp.servers.prompts.get",
        server_id,
        name,
        arguments: args,
      }).then((r) => ({
        description: r.description,
        messages: r.messages,
      })),

    // ── MCP server (Gilbert-as-MCP client registrations) ──────────

    listMcpClients: () =>
      rpc<{ clients: McpServerClient[] }>({ type: "mcp.clients.list" })
        .then((r) => r.clients),

    getMcpClient: (client_id: string) =>
      rpc<{ client: McpServerClient }>({
        type: "mcp.clients.get",
        client_id,
      }).then((r) => r.client),

    createMcpClient: (draft: McpServerClientDraft) =>
      rpc<{ client: McpServerClient; token: string }>({
        type: "mcp.clients.create",
        client: draft,
      }),

    updateMcpClient: (
      client_id: string,
      patch: Partial<McpServerClientDraft> & { active?: boolean },
    ) =>
      rpc<{ client: McpServerClient }>({
        type: "mcp.clients.update",
        client_id,
        client: patch,
      }).then((r) => r.client),

    deleteMcpClient: (client_id: string) =>
      rpc<{ client_id: string }>({
        type: "mcp.clients.delete",
        client_id,
      }),

    rotateMcpClientToken: (client_id: string) =>
      rpc<{ client: McpServerClient; token: string }>({
        type: "mcp.clients.rotate_token",
        client_id,
      }),

    previewMcpClientTools: (
      owner_user_id: string,
      profile_name: string,
    ) =>
      rpc<{
        owner_user_id: string;
        profile_name: string;
        tool_count: number;
        tools: {
          name: string;
          description: string;
          required_role: string;
        }[];
      }>({
        type: "mcp.clients.preview_tools",
        owner_user_id,
        profile_name,
      }),

    // ── Workspace Files ─────────────────────────────────────────────

    listWorkspaceFiles: (conversationId: string) =>
      rpc<{
        conversation_id: string;
        uploads: WorkspaceFile[];
        outputs: WorkspaceFile[];
        scratch: WorkspaceFile[];
      }>({
        type: "workspace.files.list",
        conversation_id: conversationId,
      }),

    pinWorkspaceFile: (fileId: string, pinned: boolean) =>
      rpc<{ file_id: string; pinned: boolean }>({
        type: "workspace.files.pin",
        file_id: fileId,
        pinned,
      }),

    deleteWorkspaceFile: (fileId: string) =>
      rpc<{ file_id: string }>({
        type: "workspace.files.delete",
        file_id: fileId,
      }),

    downloadWorkspaceFile: (
      path: string,
      conversationId: string,
    ) =>
      rpc<{
        filename: string;
        size: number;
        content_base64: string;
        media_type?: string;
      }>({
        type: "workspace.download",
        path,
        conversation_id: conversationId,
      }),

    // ── Proposals ─────────────────────────────────────────────────

    listProposals: (params?: { status?: string; kind?: string; limit?: number }) =>
      rpc<ProposalsListResult>({ type: "proposals.list", ...params }),

    getProposal: (proposalId: string) =>
      rpc<{ proposal: Proposal }>({ type: "proposals.get", proposal_id: proposalId })
        .then((r) => r.proposal),

    updateProposalStatus: (proposalId: string, status: string) =>
      rpc<{ proposal: Proposal }>({
        type: "proposals.update_status",
        proposal_id: proposalId,
        status,
      }).then((r) => r.proposal),

    addProposalNote: (proposalId: string, note: string) =>
      rpc<{ proposal: Proposal }>({
        type: "proposals.add_note",
        proposal_id: proposalId,
        note,
      }).then((r) => r.proposal),

    deleteProposal: (proposalId: string) =>
      rpc<{ proposal_id: string; status: string }>({
        type: "proposals.delete",
        proposal_id: proposalId,
      }),

    triggerProposalReflection: () =>
      rpc<{ status: "started" | "already_running" | "disabled" }>({
        type: "proposals.trigger_reflection",
      }),

    triggerProposalHarvest: () =>
      rpc<{ status: "started" | "already_running" | "disabled" }>({
        type: "proposals.trigger_harvest",
      }),

    listProposalCycles: (params?: { kind?: string; limit?: number }) =>
      rpc<ProposalsListCyclesResult>({ type: "proposals.list_cycles", ...params }),

    // ── Notifications ─────────────────────────────────────────────

    listNotifications: (filter?: { read?: boolean; source?: string; since?: string }, limit = 100) =>
      rpc<NotificationListResult>({
        type: "notification.list",
        ...(filter ? { filter } : {}),
        limit,
      }),

    markNotificationRead: (notificationId: string) =>
      rpc<{ ok: boolean; error?: string }>({
        type: "notification.mark_read",
        notification_id: notificationId,
      }),

    markAllNotificationsRead: () =>
      rpc<{ count: number }>({ type: "notification.mark_all_read" }),

    deleteNotification: (notificationId: string) =>
      rpc<{ ok: boolean; error?: string }>({
        type: "notification.delete",
        notification_id: notificationId,
      }),

    // ── Autonomous Agent ──────────────────────────────────────────

    listGoals: () =>
      rpc<{ goals: Goal[] }>({ type: "agent.goal.list" })
        .then((r) => r.goals),

    getGoal: (goalId: string) =>
      rpc<{ ok: boolean; goal?: Goal; error?: string }>({
        type: "agent.goal.get",
        goal_id: goalId,
      }),

    createGoal: (payload: GoalCreatePayload) =>
      rpc<{ ok: boolean; goal?: Goal; error?: string }>({
        type: "agent.goal.create",
        ...payload,
      }),

    updateGoal: (goalId: string, payload: GoalUpdatePayload) =>
      rpc<{ ok: boolean; goal?: Goal; error?: string }>({
        type: "agent.goal.update",
        goal_id: goalId,
        ...payload,
      }),

    deleteGoal: (goalId: string) =>
      rpc<{ ok: boolean; error?: string }>({
        type: "agent.goal.delete",
        goal_id: goalId,
      }),

    runGoalNow: (goalId: string) =>
      rpc<{ ok: boolean; run?: AgentRun; error?: string }>({
        type: "agent.goal.run_now",
        goal_id: goalId,
      }),

    listAgentRuns: (goalId: string, limit = 100) =>
      rpc<{ ok: boolean; runs?: AgentRun[]; error?: string }>({
        type: "agent.run.list",
        goal_id: goalId,
        limit,
      }),

    getAgentRun: (runId: string) =>
      rpc<{ ok: boolean; run?: AgentRun; error?: string }>({
        type: "agent.run.get",
        run_id: runId,
      }),

    authorGoalInstruction: (
      goalId: string,
      currentText: string,
      instruction: string,
      aiProfile?: string,
    ) =>
      rpc<{ ok: boolean; new_text?: string; profile_used?: string; error?: string }>({
        type: "agent.goal.author_instruction",
        goal_id: goalId,
        current_text: currentText,
        instruction,
        ai_profile: aiProfile ?? "",
      }),

  }), [rpc, rpcWithRef]);
}
