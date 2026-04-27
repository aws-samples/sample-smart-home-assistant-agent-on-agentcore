import { defineConfig, devices } from "@playwright/test";

// All run-parameters flow through env vars so the harness shell script
// can re-invoke the same spec with different region/mode/rounds without
// code changes.
export default defineConfig({
  testDir: ".",
  timeout: 120_000,
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    // Grant mic upfront so no permission prompt blocks the voice button.
    permissions: ["microphone"],
    // Headless is fine — CDP works the same.
    headless: true,
    launchOptions: {
      // --use-fake-ui-for-media-stream prevents any mic prompt.
      // --use-fake-device-for-media-stream supplies silent PCM so Nova Sonic
      // gets audio frames (it needs *some* frames to keep the WS live).
      args: [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
      ],
    },
  },
});
