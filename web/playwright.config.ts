import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  use: { baseURL: 'http://127.0.0.1:5173', trace: 'off' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command:
        'uv run --frozen uvicorn app.web_poc.main:app --host 127.0.0.1 --port 8001 --no-access-log',
      cwd: '..',
      url: 'http://127.0.0.1:8001/api/web/health',
      reuseExistingServer: !process.env.CI,
    },
    { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: !process.env.CI },
  ],
})
