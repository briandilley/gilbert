import { useCallback, useEffect, useRef, useState } from "react";
import { useEventBus } from "./useEventBus";
import { useAuth } from "./useAuth";
import type { GilbertEvent } from "@/types/events";

/**
 * Browser-native notifications for @-mentions.
 *
 * Mounted once near the app root. Subscribes to
 * ``notification.received`` events with ``source === "chat.mention"``
 * and fires a system notification when:
 *
 *   - The user has granted permission, AND
 *   - The browser tab isn't focused on the mentioning conversation
 *     (visible + on the right conversation = the user already sees it).
 *
 * Permission is requested lazily — the first time a mention arrives we
 * trigger the OS prompt. Asking up-front is the textbook UX anti-pattern
 * (users say "no" to a context-free prompt). We surface "you have an
 * unrequested mention" in the SPA bell while permission is pending so
 * nothing is lost.
 *
 * Returns helpers for callers that want to control the permission
 * state from a settings page:
 *
 *   - ``permission``: current value of ``Notification.permission`` (or
 *     ``"unsupported"`` on browsers without the API).
 *   - ``requestPermission()``: explicit opt-in trigger.
 */
export function useBrowserNotifications(opts: {
  /** Conversation id the user is currently viewing. Notifications
   *  for THIS conversation are suppressed when the tab is also
   *  visible — the user is already looking at the message. */
  activeConversationId?: string | null;
}): {
  permission: NotificationPermission | "unsupported";
  requestPermission: () => Promise<NotificationPermission | "unsupported">;
} {
  const supported = typeof window !== "undefined" && "Notification" in window;
  const [permission, setPermission] = useState<
    NotificationPermission | "unsupported"
  >(supported ? Notification.permission : "unsupported");
  const { user } = useAuth();
  // Hold the latest active conversation in a ref so the event handler
  // (which closes over its own copy of the deps the first time it
  // mounts) always sees the current value.
  const activeRef = useRef<string | null | undefined>(opts.activeConversationId);
  useEffect(() => {
    activeRef.current = opts.activeConversationId;
  }, [opts.activeConversationId]);

  const requestPermission = useCallback(async () => {
    if (!supported) return "unsupported";
    if (Notification.permission !== "default") {
      return Notification.permission;
    }
    const result = await Notification.requestPermission();
    setPermission(result);
    return result;
  }, [supported]);

  const handleMention = useCallback(
    (event: GilbertEvent) => {
      if (!supported) return;
      const data = event.data as Record<string, unknown>;
      if (data.source !== "chat.mention") return;
      // Don't fire if the user authored this mention themselves
      // (shouldn't happen because the backend filters self-mentions,
      // but defense in depth).
      if (data.user_id !== user?.user_id) return;
      const isFocusedHere =
        document.visibilityState === "visible" &&
        typeof activeRef.current === "string" &&
        activeRef.current ===
          ((data.source_ref as Record<string, unknown> | undefined)
            ?.conversation_id ?? "");
      if (isFocusedHere) return;

      const trigger = (perm: NotificationPermission) => {
        if (perm !== "granted") return;
        const sourceRef =
          (data.source_ref as Record<string, unknown> | undefined) ?? {};
        const authorName = String(sourceRef.author_name ?? "Someone");
        try {
          // ``tag`` groups successive mentions from the same room so
          // a busy chat doesn't spawn a dozen OS toasts — newer
          // notifications replace older ones in-place. ``renotify``
          // would force a re-buzz, but its TypeScript lib def is
          // out of date and it's nice-to-have, not load-bearing.
          const notif = new Notification(`${authorName} mentioned you`, {
            body: typeof data.message === "string" ? data.message : "",
            tag: `chat.mention.${sourceRef.conversation_id ?? ""}`,
          });
          notif.onclick = () => {
            // Focus the tab + deep-link to the conversation. The
            // ``/c/<id>`` route is what the SPA uses for direct chat
            // navigation; the bell already navigates this way.
            window.focus();
            const cid = sourceRef.conversation_id;
            if (typeof cid === "string" && cid) {
              window.location.hash = `#/c/${cid}`;
            }
            notif.close();
          };
        } catch {
          // Notification constructor can throw in some embedded
          // contexts (PWA-without-perm-shim, restricted iframes).
          // Swallowing keeps the bell as the working fallback.
        }
      };

      if (Notification.permission === "default") {
        // Lazy permission request — first mention triggers the prompt.
        void Notification.requestPermission().then((perm) => {
          setPermission(perm);
          trigger(perm);
        });
      } else {
        trigger(Notification.permission);
      }
    },
    [supported, user?.user_id],
  );

  useEventBus("notification.received", handleMention);

  return { permission, requestPermission };
}
