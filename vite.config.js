import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const demo = mode === 'demo';
  return {
    plugins: [react()],
    // The demo build swaps the trading database's client for sample data. Only src/supabaseClient.js is
    // swapped, so the playground's own Supabase project still gets the real client library.
    resolve: demo
      ? { alias: [{ find: /^\.\/supabaseClient(\.js)?$/, replacement: fileURLToPath(new URL('./src/demo/fakeSupabase.js', import.meta.url)) }] }
      : {},
    build: {
      // Split large, rarely-changing dependencies into their own chunks so
      // browsers cache them across deploys and download them in parallel.
      rollupOptions: {
        output: {
          manualChunks: {
            react: ['react', 'react-dom'],
            charts: ['chart.js', 'react-chartjs-2'],
            supabase: ['@supabase/supabase-js'],
          },
        },
      },
      // Vendor chunks legitimately exceed the default 500 kB warning.
      chunkSizeWarningLimit: 700,
    },
  };
});
