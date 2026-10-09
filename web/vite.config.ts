import react from '@vitejs/plugin-react';
import path from 'node:path';
import { defineConfig } from 'vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

// One self-contained HTML file: Lens serves it as is, and the offline demo embeds data in it.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: { alias: { '@': path.resolve(__dirname) } },
  // In development, the API is a running Lens (LENS_DEV_URL, default :8900).
  server: { proxy: { '/api': process.env.LENS_DEV_URL ?? 'http://127.0.0.1:8900' } },
});
