import path from "path";
import react from "@vitejs/plugin-react";
// From vitest rather than vite, which is what types the `test` block below.
import { defineConfig } from "vitest/config";

// https://vite.dev/config/
export default defineConfig({
  base: "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  // For the pure helpers the dashboard is starting to accumulate — graph layering,
  // and the state derivation beside it. No DOM: anything that needs one is a Cypress
  // spec against the real server (RFC 0006 §10).
  test: {
    include: ["src/**/*.test.ts"],
    environment: "node",
  },
});
