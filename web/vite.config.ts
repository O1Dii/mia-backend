import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev and test ports are configurable so parallel checkouts do not collide.
const webPort = Number(process.env.MIA_WEB_PORT ?? 5173)
const apiUrl = process.env.MIA_API_URL ?? 'http://127.0.0.1:8000'
// changeOrigin stays off: the API compares the Origin of cookie-authenticated writes with Host.
const proxy = { '/v1': { target: apiUrl, changeOrigin: false } }

export default defineConfig({
  base: process.env.MIA_BASE_PATH ? `${process.env.MIA_BASE_PATH}/` : '/',
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('index.html', import.meta.url)),
      },
      output: {
        manualChunks(id) {
          if (id.includes('/node_modules/zod/')) return 'validation'
          if (
            id.includes('/node_modules/motion/') ||
            id.includes('/node_modules/framer-motion/') ||
            id.includes('/node_modules/motion-dom/') ||
            id.includes('/node_modules/motion-utils/')
          )
            return 'motion'
          if (
            id.includes('/node_modules/react/') ||
            id.includes('/node_modules/react-dom/') ||
            id.includes('/node_modules/scheduler/')
          )
            return 'react'
        },
      },
    },
  },
  server: { port: webPort, strictPort: true, proxy: { ...proxy, '/__e2e': proxy['/v1'] } },
  preview: { port: 4173, proxy },
})
