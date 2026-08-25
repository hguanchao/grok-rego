import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { createLogger, defineConfig } from 'vite'

/** 静音 Vite 启动 banner（ready / Local / press h 等 info 输出），保留 warn/error */
const logger = createLogger()
logger.info = () => {}

/** 开发态将 /api 代理到后端管理 API（默认 8787） */
export default defineConfig({
  customLogger: logger,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5274,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
      '/zen': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
      '/grok': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
