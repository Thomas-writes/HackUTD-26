import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'SSD Health Dashboard',
        short_name: 'SSD Health',
        display: 'standalone',
        background_color: '#0f172a',
        theme_color: '#0f172a',
      icons: [
        { src: 'web-app-manifest-192x192.png', sizes: '192x192', type: 'image/png' },
        { src: 'web-app-manifest-512x512.png', sizes: '512x512', type: 'image/png' },
        { src: 'web-app-manifest-512x512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' }
      ]
      }
    })
  ],
  server: {
    proxy: {
      '/demo/fleet': 'http://127.0.0.1:8000',
      '/telemetry-health': {
        target: 'http://127.0.0.1:8000',
        rewrite: () => '/healthz',
      },
    },
  },
  test: { environment: 'node' }, // DOM-specific files opt in to jsdom below
  preview: { 
    allowedHosts: true, 
    proxy: {
        '/v1': { target: 'http://127.0.0.1:3000', ws: true, changeOrigin: true }
      }
  }
});
