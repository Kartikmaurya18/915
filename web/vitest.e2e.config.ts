import { defineConfig } from "vitest/config";

// Live end-to-end checks against a running backend (see tests/e2e/live.test.ts). Not part of `npm test`.
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/e2e/**/*.test.ts"],
    testTimeout: 45_000,
    hookTimeout: 20_000,
    fileParallelism: false,
  },
});
