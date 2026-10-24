import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the API runs separately (`careops serve`); proxy /api to it.
// In production FastAPI serves the built app from ui/dist, so no proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
})
