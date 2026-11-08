import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { notices } from './scripts/notices.ts';

const authFlag = process.env.EAI_FRONTEND_AUTH_ENABLED ?? 'false';
if (authFlag !== 'true' && authFlag !== 'false') throw new Error('EAI_FRONTEND_AUTH_ENABLED must be true or false.');
const root = fileURLToPath(new URL('.', import.meta.url));
const localHttps = process.env.EAI_LOCAL_HTTPS === 'true';
const localTls = localHttps ? {
  key: readFileSync(fileURLToPath(new URL('../local/app-runtime/localhost-key.pem', import.meta.url))),
  cert: readFileSync(fileURLToPath(new URL('../local/app-runtime/localhost-cert.pem', import.meta.url))),
} : undefined;

export default defineConfig({
  root,
  plugins: [react(), notices(root)],
  define: { __EAI_AUTH_ENABLED__: authFlag === 'true' },
  // Bootstrap consumes no .env files or browser-exposed custom environment values.
  envDir: false,
  envPrefix: [],
  server: {
    host: '127.0.0.1', port: localHttps ? 443 : 5173, strictPort: true,
    https: localTls,
    // Local development only: keep browser API requests on the frontend origin.
    proxy: { '/api': { target: 'http://127.0.0.1:8000' } },
  },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true },
  build: { outDir: 'dist', sourcemap: false },
});
