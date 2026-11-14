import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served by the gateway at /pulse/ (PULSE_STATIC_DIR = frontend/dist); API calls go to the
// relative /pulse/api, which the gateway proxies to Pulse on 127.0.0.1:7710.
// `npm run dev` proxies the same path straight to the local API.
export default defineConfig({
  base: '/pulse/',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 1500 },
  server: {
    host: '127.0.0.1',
    proxy: {
      '/pulse/api': {
        target: 'http://127.0.0.1:7710',
        ws: true,
        rewrite: (path) => path.replace(/^\/pulse\/api/, ''),
      },
    },
  },
})
