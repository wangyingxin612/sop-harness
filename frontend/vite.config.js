import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-time proxy so the frontend (5173) can call the backend (8000) without
// CORS friction. In production the built frontend is served BY the FastAPI
// app (see backend/app/api/main.py's StaticFiles mount), so this proxy is
// dev-only and irrelevant to the Docker image.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
