import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  // Reads .env from the repo root (one dir up) -- one source of truth for VITE_SUPABASE_URL/
  // VITE_SUPABASE_ANON_KEY, no duplicated/stale copy inside dashboard/.
  envDir: path.resolve(import.meta.dirname, '..'),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: {
      // Local FastAPI backend (dashboard/server) -- score-quote, assign, invoices, sim playback.
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: true,
      },
    },
  },
})
