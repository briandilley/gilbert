import path from "path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Standalone test config — deliberately NOT importing vite.config.ts so the
// PWA/service-worker plugin and dev proxy don't load under jsdom.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
