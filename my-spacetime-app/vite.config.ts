import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/demo/fleet': 'http://127.0.0.1:8000',
      '/telemetry-health': {
        target: 'http://127.0.0.1:8000',
        rewrite: () => '/healthz',
      },
    },
  },
});
