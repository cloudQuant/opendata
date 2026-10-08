import { defineConfig, devices } from '@playwright/test'

const baseURL = 'http://127.0.0.1:33567'
const productionDistDir = process.env.PRODUCTION_DIST_DIR ?? 'dist'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'production-login.smoke.ts',
  fullyParallel: false,
  reporter: 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  webServer: {
    command: `npm run preview -- --host 127.0.0.1 --port 33567 --strictPort --outDir "${productionDistDir}"`,
    url: `${baseURL}/login`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
