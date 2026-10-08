import { defineConfig, devices } from '@playwright/test'

function resolvePreviewPort(value: string | undefined): number {
  const rawPort = value ?? '4173'
  if (!/^(0|[1-9]\d*)$/.test(rawPort)) {
    throw new Error('OPENDATA_E2E_PORT must be a canonical decimal integer from 1 to 65535')
  }

  const port = Number(rawPort)
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new Error('OPENDATA_E2E_PORT must be a canonical decimal integer from 1 to 65535')
  }
  return port
}

const previewPort = resolvePreviewPort(process.env.OPENDATA_E2E_PORT)
const previewUrl = `http://127.0.0.1:${previewPort}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: previewUrl,
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: `npm run build && npm run preview -- --host 127.0.0.1 --port ${previewPort} --strictPort`,
    url: previewUrl,
    reuseExistingServer: false,
    timeout: 120000,
  },
})
