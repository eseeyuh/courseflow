/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import { defineConfig } from "@playwright/test";

// The golden-path test drives the real frontend against the real offline
// demo API (scripts/demo-api.sh). Both servers are reused when running.
// Runs are serial: they share one demo database. E2E_BASE_URL points it at
// an already running demo instead (e.g. infra/demo/compose.yml), and then
// starts no servers.
const target = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: target ?? "http://localhost:3000",
    // The installed Chrome: no browser download needed.
    channel: process.env.PLAYWRIGHT_CHANNEL ?? "chrome",
    viewport: { width: 1360, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: target ? [] : [
    {
      command: "sh ../scripts/demo-api.sh",
      url: "http://127.0.0.1:8000/api/v1/health",
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: "npm run dev",
      url: "http://localhost:3000",
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
