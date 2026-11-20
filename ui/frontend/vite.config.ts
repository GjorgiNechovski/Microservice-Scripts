import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The backend serves the built page from ui/backend/wwwroot. During development,
// `npm run dev` serves it from here and passes /api on to the backend
// (see ui/README.md).
export default defineConfig({
  plugins: [react()],
  // served from this machine only, so one larger bundle is fine
  build: { outDir: '../backend/wwwroot', emptyOutDir: true, chunkSizeWarningLimit: 2000 },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:5199', changeOrigin: true } } },
  test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], css: false },
});
