import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue()],
    // 构建期常量：仅 `vite --mode demo` 为 true；生产构建中演示分支被整体消除。
    define: { __HP_DEMO__: JSON.stringify(command === 'serve' && mode === 'demo') },
    server: {
      port: Number(env.WORKBENCH_PORT || 5173),
      strictPort: true,
      proxy: {
        '/api': {
          target: env.WORKBENCH_API_TARGET || 'http://127.0.0.1:8000',
          changeOrigin: false,
        },
      },
    },
    build: { chunkSizeWarningLimit: 700 },
  }
})
