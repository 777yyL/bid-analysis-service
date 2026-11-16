import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import {fileURLToPath, URL} from 'node:url';
export default defineConfig({
  base: '/ui/', plugins: [react(),tailwindcss()],
  resolve:{alias:{'@':fileURLToPath(new URL('./src',import.meta.url))}},
  server: {proxy: {'/api': 'http://127.0.0.1:8100', '/knowledge': 'http://127.0.0.1:8100', '/config': 'http://127.0.0.1:8100'}},
  build: {chunkSizeWarningLimit: 1200}
});
