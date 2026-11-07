import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发时 `npm run dev` 起在 5173，接口转发给 web_main.py 起的后端（8765）。
// 打包后前端是 dist/ 里的静态文件，由同一个 Python 进程直接提供，不经过这里。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  build: { chunkSizeWarningLimit: 2000 },
})
