import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'workbench.spec.ts',
  workers: 1,
  timeout: 45000,
  reporter: 'list',
  use: {
    baseURL: process.env.WORKBENCH_URL || 'http://127.0.0.1:5173',
    viewport: { width: 1440, height: 1000 },
    ignoreHTTPSErrors: true,
    screenshot: 'only-on-failure',
    trace: 'off',
  },
})
