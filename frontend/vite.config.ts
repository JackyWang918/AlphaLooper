import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 18761,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:18760', ws: true },
    },
  },
})
