import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  base: '/ui/', plugins: [react()],
  server: {proxy: {'/api': 'http://127.0.0.1:8100', '/knowledge': 'http://127.0.0.1:8100', '/config': 'http://127.0.0.1:8100'}},
  build: {chunkSizeWarningLimit: 1200}
});
