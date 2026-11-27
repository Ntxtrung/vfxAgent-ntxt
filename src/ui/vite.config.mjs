import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, '../../', ''), ...process.env }
  const core =
    env.CORE_URL ||
    `http://${env.CORE_HOST || '127.0.0.1'}:${env.CORE_PORT || '8765'}`
  const proxy = { '^/api(?:/|$)': core, '^/health$': core }
  return {
    server: {
      host: env.UI_HOST || '127.0.0.1',
      port: Number(env.UI_PORT || 5173),
      strictPort: true,
      proxy,
    },
    preview: { proxy },
  }
})
