import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/',
  plugins: [react()],
  server: {
    host: true,
    port: 3000,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // Rewrite `/api/chat` → `/chat` so the frontend uses one base for dev and prod.
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
});
