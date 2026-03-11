import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 3002,
    host: '0.0.0.0',   // Listen on all interfaces (IPv4 + IPv6)
    strictPort: true,   // Fail instead of trying random ports
    proxy: {
      // GitHub API routes — keep /api/github prefix (daemon expects it)
      '/api/github': {
        target: 'http://localhost:5555',
      },
      // Legacy fallback to daemon HTTP during transition
      '/api': {
        target: 'http://localhost:5555',
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      // Serve uploaded files from daemon
      '/uploads': {
        target: 'http://localhost:5555',
      }
    }
  }
})
