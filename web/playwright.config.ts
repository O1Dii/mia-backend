import { defineConfig, devices } from '@playwright/test'

// The tests run the real backend wiring (tests/support/e2e_server.py) on a disposable PostgreSQL and
// Redis: set MIA_TEST_DATABASE_URL and MIA_TEST_REDIS_URL (see web/README.md). Ports are overridable.
const webPort = Number(process.env.MIA_WEB_PORT ?? 5173)
const apiPort = Number(process.env.MIA_API_PORT ?? 8100)
const origin = `http://127.0.0.1:${webPort}`

for (const name of ['MIA_TEST_DATABASE_URL', 'MIA_TEST_REDIS_URL'])
  if (!process.env[name]) throw new Error(`${name} is required for the web tests`)

export default defineConfig({
  testDir: './tests',
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: { baseURL: origin, trace: 'off' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `uv run --frozen python web/tests/support/e2e_server.py --port ${apiPort} --public-origin ${origin}`,
      cwd: '..',
      url: `http://127.0.0.1:${apiPort}/health/live`,
      reuseExistingServer: false,
      timeout: 180_000,
    },
    {
      command: 'npm run dev',
      url: origin,
      reuseExistingServer: false,
      env: {
        ...(process.env as Record<string, string>),
        MIA_WEB_PORT: String(webPort),
        MIA_API_URL: `http://127.0.0.1:${apiPort}`,
      },
    },
  ],
})
