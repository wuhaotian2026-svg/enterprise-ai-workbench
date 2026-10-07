import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { assetsInlineLimit: 0 },
  server: { host: "127.0.0.1", port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  test: { environment: "jsdom", setupFiles: "./src/test/setup.ts", css: true, exclude: ["tests/e2e/**", "node_modules/**", "**/backup_*/**"] },
});
