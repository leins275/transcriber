/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],

  // Tauri expects a fixed dev server port; fail if it's unavailable.
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },

  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    globals: false,
  },
});
