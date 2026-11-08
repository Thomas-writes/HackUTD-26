import { defineConfig } from 'vite'
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
  preview: { allowedHosts: true }
})
