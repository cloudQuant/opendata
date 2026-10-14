import { defineConfig, devices } from '@playwright/test'

function loopbackPort(name: string): number {
  const value = process.env[name]
  if (!value || !/^[1-9]\d{0,4}$/.test(value)) {
    throw new Error(`${name} must be a port on 127.0.0.1`)
  }
  const port = Number(value)
  if (port > 65535) throw new Error(`${name} is outside the TCP port range`)
  return port
}

const frontendPort = loopbackPort('OPENDATA_AC21_FRONTEND_PORT')
const baseURL = `http://127.0.0.1:${frontendPort}`
const backendUrl = process.env.OPENDATA_AC21_BACKEND_URL
if (!backendUrl || new URL(backendUrl).hostname !== '127.0.0.1') {
  throw new Error('The AC21 API must be on the test-owned 127.0.0.1 backend')
}

export default defineConfig({
  testDir: '.',
  testMatch: 'script-download-backend.spec.ts',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: 'line',
  timeout: 180000,
  expect: { timeout: 30000 },
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    headless: true,
    trace: 'off',
    screenshot: 'off',
  },
  webServer: {
    command: `npm run dev -- --config e2e/script-download-backend-vite.config.ts --host 127.0.0.1 --port ${frontendPort} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120000,
  },
})
