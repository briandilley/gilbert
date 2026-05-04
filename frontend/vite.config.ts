import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
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
