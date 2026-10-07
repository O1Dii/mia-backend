import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: process.env.MIA_BASE_PATH ? `${process.env.MIA_BASE_PATH}/` : '/',
  plugins: [react()],
  build: {
    rollupOptions: {
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
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api/web': { target: 'http://127.0.0.1:8001', changeOrigin: true } },
  },
  preview: {
    port: 4173,
    proxy: { '/api/web': { target: 'http://127.0.0.1:8001', changeOrigin: true } },
  },
})
