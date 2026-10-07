// Browser tests: drive the real React app in Microsoft Edge against the LOCAL
// stack (backend/local/dev_server.py + mock AWS). Not a real AWS test.
import { defineConfig } from '@playwright/test'

const python = process.platform === 'win32'
  ? '../backend/.venv/Scripts/python.exe'
  : '../backend/.venv/bin/python'

// INTERVIEW:
// Playwright drives a real browser (Edge) and clicks through the app like a user would:
// these are end-to-end UI tests. By default they run against the local mock backend;
// with E2E_EXTERNAL_SERVERS=1 and "npm run dev:aws" running, the same tests run
// against the real AWS backend.
export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  workers: 1,
  reporter: 'list',
  globalSetup: './e2e/global-setup.js',
  use: {
    baseURL: 'http://localhost:5173',
    channel: 'msedge', // uses the Edge already installed on Windows (no browser download)
    acceptDownloads: true,
    trace: 'retain-on-failure',
  },
  // Started automatically unless already running. Set E2E_EXTERNAL_SERVERS=1 to skip
  // (e.g. when testing against real AWS with "npm run dev:aws" already running).
  webServer: process.env.E2E_EXTERNAL_SERVERS ? undefined : [
    {
      command: `${python} ../backend/local/dev_server.py`,
      port: 8000,
      reuseExistingServer: true,
      env: { DOWNLOAD_URL_TTL_SECONDS: '30', PYTHONUNBUFFERED: '1' },
    },
    { command: 'npm run dev', port: 5173, reuseExistingServer: true },
  ],
})
