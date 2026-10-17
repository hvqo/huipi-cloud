import { defineConfig } from '@playwright/test'

/**
 * 演示模式 E2E：完全本地运行（无后端、无 OIDC、无数据库），
 * 通过 `npm run dev:demo` 启动合成数据环境后驱动教师与管理员全流程。
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: 'demo.spec.ts',
  workers: 1,
  timeout: 60_000,
  reporter: 'list',
  use: {
    baseURL: process.env.DEMO_URL || 'http://127.0.0.1:5273',
    viewport: { width: 1440, height: 1000 },
  },
  webServer: {
    command: 'WORKBENCH_PORT=5273 npm run dev:demo',
    url: 'http://127.0.0.1:5273/login',
    reuseExistingServer: true,
    timeout: 60_000,
  },
})
