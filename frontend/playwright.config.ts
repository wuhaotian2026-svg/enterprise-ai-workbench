import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  testIgnore: ["**/backup_*/**"],
  workers: 1,
  outputDir: "../output/playwright/test-results",
  reporter: [["html", { outputFolder: "../output/playwright/report", open: "never" }], ["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8080",
    browserName: "chromium",
    channel: "chrome",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
