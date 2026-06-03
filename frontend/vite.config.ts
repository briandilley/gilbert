import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { VitePWA } from "vite-plugin-pwa";
import path from "path";

// Dark theme canvas — `oklch(0.10 0 0)` in src/index.css, which is
// effectively #0a0a0a. Used as both the PWA theme_color and
// background_color so the splash/title-bar matches the SPA.
const THEME_COLOR = "#0a0a0a";

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      // "prompt": the page surfaces an in-app "Update available" toast
      // (see src/components/PwaUpdatePrompt.tsx). Silent auto-reloads
      // would yank state out from under an active chat.
      registerType: "prompt",
      // Register the SW ourselves from main.tsx so we can wire the
      // update-prompt UX (registerSW from virtual:pwa-register).
      injectRegister: false,
      // Custom SW is required for push handling — generateSW can't
      // express the `push`/`notificationclick` logic cleanly.
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      injectManifest: {
        // The default 2 MiB ceiling is enough for the SPA shell;
        // large vendor chunks (recharts, highlight.js) blow past it.
        // Bump to 5 MiB and revisit if the bundle grows further.
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
      },
      // The SW interferes with HMR; keep it off in dev. Test the PWA
      // via `npm run build && npm run preview`.
      devOptions: {
        enabled: false,
      },
      manifest: {
        name: "Gilbert",
        short_name: "Gilbert",
        description: "AI assistant for home and business automation",
        display: "standalone",
        start_url: "/",
        scope: "/",
        theme_color: THEME_COLOR,
        background_color: THEME_COLOR,
        icons: [
          {
            src: "/icons/gilbert-192.png",
            sizes: "192x192",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "/icons/gilbert-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "/icons/gilbert-512-maskable.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
    }),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    fs: {
      // Plugins live under <repo>/std-plugins/*/frontend/ etc. — one
      // directory up from the Vite project root (frontend/). Allow
      // the dev server to read those files so the per-plugin glob
      // auto-loader in src/plugins/index.ts can resolve them.
      allow: [path.resolve(__dirname, "..")],
    },
    proxy: {
      "/api": "http://localhost:8000",
      "/auth": "http://localhost:8000",
      "/chat": "http://localhost:8000",
      "/documents": "http://localhost:8000",
      "/entities": "http://localhost:8000",
      "/inbox": "http://localhost:8000",
      "/roles": "http://localhost:8000",
      "/screens": "http://localhost:8000",
      "/system": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/output": "http://localhost:8000",
      "/static": "http://localhost:8000",
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
});
