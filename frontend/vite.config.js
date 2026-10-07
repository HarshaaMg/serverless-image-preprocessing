import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// During local development, /api requests are forwarded to the local dev server
// (backend/local/dev_server.py). In production the app calls API Gateway directly
// using VITE_API_BASE_URL.
// INTERVIEW:
// The dev-server proxy forwards /api/* from http://localhost:5173 to the local mock
// backend on port 8000. The browser therefore sees same-origin requests, so no CORS
// setup is needed during local development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  // Vitest (unit tests in src/*.test.js) reuses this config; plain Node, no browser.
  test: {
    include: ['src/**/*.test.{js,jsx}'],
    environment: 'node',
  },
})
