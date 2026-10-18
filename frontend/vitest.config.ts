import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
export default defineConfig({
  plugins: [vue()],
  define: {
    'import.meta.env.VITE_OIDC_AUTHORITY': '""',
    'import.meta.env.VITE_OIDC_CLIENT_ID': '""',
    'import.meta.env.VITE_OIDC_AUDIENCE': '""',
    'import.meta.env.VITE_OIDC_RESOURCE': '""',
    'import.meta.env.VITE_DEMO_MODE': '"false"',
    __HP_DEMO__: 'false',
  },
  test: { environment: 'jsdom', include: ['tests/**/*.test.ts'], restoreMocks: true },
})
