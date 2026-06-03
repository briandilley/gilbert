/// <reference lib="webworker" />
/**
 * Gilbert PWA service worker.
 *
 * Custom worker (vite-plugin-pwa "injectManifest" strategy) so we can
 * own the `push` and `notificationclick` handlers. Routing rules:
 *
 *   - `/api/*` and `/ws*`  → NetworkOnly. These carry authenticated
 *                            mutations and the WebSocket; caching them
 *                            would corrupt session state.
 *   - navigation requests  → NetworkFirst with a short timeout so the
 *                            app shell stays fresh when the network is
 *                            available.
 *   - precached assets     → served from cache via the workbox
 *                            precache (manifest injected at build).
 *
 * Update flow:
 *
 *   - We do NOT call `skipWaiting()` on install. Instead the page
 *     surfaces an in-app "Update available — Reload" prompt
 *     (`PwaUpdatePrompt.tsx`). Clicking the prompt posts
 *     `{type: "SKIP_WAITING"}` to this worker, which then activates
 *     and the page reloads.
 *
 * Push handling:
 *
 *   - The push payload is expected to be JSON of shape
 *     `{title, body, icon?, badge?, tag?, data?: {url?, notification_id?}}`.
 *     We render whatever we're given and fall back to a generic
 *     "Gilbert / (empty notification)" if the payload is missing or
 *     not JSON. The worker is intentionally generic — it does not
 *     know about specific Gilbert notification routes.
 */

import { clientsClaim } from "workbox-core";
import {
  cleanupOutdatedCaches,
  createHandlerBoundToURL,
  precacheAndRoute,
} from "workbox-precaching";
import { NavigationRoute, registerRoute } from "workbox-routing";
import { NetworkFirst, NetworkOnly } from "workbox-strategies";

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>;
};

// ── Precache the SPA shell built by Vite ──────────────────────────
precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();

// ── Routing ──────────────────────────────────────────────────────
//
// API and WS: never touch the cache. The Gilbert backend is
// authenticated and stateful — a cached 200 would be a bug.
registerRoute(({ url }) => url.pathname.startsWith("/api/"), new NetworkOnly());
registerRoute(({ url }) => url.pathname.startsWith("/ws"), new NetworkOnly());

// Navigation requests (SPA shell): network-first with a tight
// timeout, falling back to the precached index.html when offline.
// vite-plugin-pwa precaches the built index under a known URL we
// resolve at activation time via createHandlerBoundToURL.
const navigationHandler = new NetworkFirst({
  cacheName: "gilbert-navigation",
  networkTimeoutSeconds: 3,
});
registerRoute(
  new NavigationRoute(navigationHandler, {
    denylist: [/^\/api\//, /^\/ws/, /^\/auth\//],
  }),
);

// Fallback: ensure /index.html resolves from the precache when the
// network is unreachable (createHandlerBoundToURL participates in the
// precache lookup so the right revisioned asset is served).
try {
  const indexFallback = createHandlerBoundToURL("/index.html");
  registerRoute(
    new NavigationRoute(indexFallback, {
      denylist: [/^\/api\//, /^\/ws/, /^\/auth\//],
    }),
  );
} catch {
  // The route above is the primary navigation handler; the fallback
  // only kicks in if /index.html happens to be in the precache
  // manifest. In dev mode (devOptions.enabled=false) this throws
  // synchronously — swallow it.
}

// ── Update prompt protocol ────────────────────────────────────────
//
// The page asks us to activate by posting `{type: "SKIP_WAITING"}`
// after the user accepts the in-app update prompt. We do not call
// skipWaiting() unconditionally — that would yank state out from
// under an active conversation.
self.addEventListener("message", (event) => {
  const data = event.data as { type?: string } | undefined;
  if (data && data.type === "SKIP_WAITING") {
    void self.skipWaiting();
  }
});

// After skipWaiting fires we DO claim clients so the new worker
// controls every open tab on the next navigation.
clientsClaim();

// ── Push handler ─────────────────────────────────────────────────
//
// Generic by design: render whatever JSON the backend hands us. No
// Gilbert-specific routing logic — that lives in the page when the
// notification is clicked.
interface GilbertPushPayload {
  title?: string;
  body?: string;
  icon?: string;
  badge?: string;
  tag?: string;
  data?: {
    url?: string;
    notification_id?: string;
    [key: string]: unknown;
  };
}

function parsePayload(event: PushEvent): GilbertPushPayload {
  if (!event.data) return {};
  try {
    return event.data.json() as GilbertPushPayload;
  } catch {
    // Best-effort plain-text fallback so a malformed payload still
    // surfaces something rather than swallowing the push silently.
    try {
      const text = event.data.text();
      return { body: text };
    } catch {
      return {};
    }
  }
}

self.addEventListener("push", (event: PushEvent) => {
  const payload = parsePayload(event);
  const title = payload.title ?? "Gilbert";
  const options: NotificationOptions = {
    body: payload.body ?? "(empty notification)",
    icon: payload.icon ?? "/icons/gilbert-192.png",
    badge: payload.badge ?? "/icons/gilbert-192.png",
    tag: payload.tag,
    data: payload.data ?? {},
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

// ── Notification click handler ───────────────────────────────────
//
// Prefer focusing an already-open Gilbert tab to avoid stacking up
// duplicate windows; only `openWindow` as a fallback.
self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  const data = (event.notification.data ?? {}) as { url?: string };
  const target = data.url ?? "/";

  event.waitUntil(
    (async () => {
      const allClients = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });

      // Prefer an existing same-origin tab. Navigate it to the
      // target URL and focus.
      for (const client of allClients) {
        if (
          client.url &&
          new URL(client.url).origin === self.location.origin
        ) {
          try {
            await client.focus();
            // navigate() is only available on WindowClient; the type
            // narrow ensures we only call it when present.
            if ("navigate" in client) {
              await (client as WindowClient).navigate(target);
            }
            return;
          } catch {
            // Continue searching other clients.
          }
        }
      }

      // No suitable tab — open a new one.
      if (self.clients.openWindow) {
        await self.clients.openWindow(target);
      }
    })(),
  );
});
