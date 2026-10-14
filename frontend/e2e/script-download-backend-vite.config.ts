import { mergeConfig } from 'vite'
import baseConfig from '../vite.config'

function requiredLoopbackUrl(name: string): string {
  const raw = process.env[name]
  if (!raw) throw new Error(`${name} is required for the AC21 backend E2E`)
  const parsed = new URL(raw)
  if (parsed.protocol !== 'http:' || parsed.hostname !== '127.0.0.1') {
    throw new Error(`${name} must target an HTTP 127.0.0.1 endpoint`)
  }
  return parsed.toString().replace(/\/$/, '')
}

const backendUrl = requiredLoopbackUrl('OPENDATA_AC21_BACKEND_URL')

const ac21RouterObserver = {
  name: 'ac21-router-observer',
  enforce: 'pre' as const,
  transform(source: string, id: string) {
    if (!id.replace(/\\/g, '/').endsWith('/src/router/index.ts')) return null

    const exportAnchor = 'export default router'
    if (!source.includes(exportAnchor)) {
      throw new Error('AC21 could not instrument the real Vue Router export')
    }

    const observer = `
if (typeof window !== 'undefined') {
  const probe = (window as any).__AC21_ROUTER_PROBE__
  if (probe) {
    router.afterEach((to, from, failure) => {
      probe.route_events.push({
        at_ms: performance.now(),
        from: from.fullPath,
        to: to.fullPath,
        failure: failure ? { type: failure.type, message: String(failure) } : null,
      })
    })
    router.onError((error) => {
      probe.router_errors.push({
        at_ms: performance.now(),
        name: error?.name ?? 'Error',
        message: String(error?.message ?? error),
      })
    })
  }
}
`
    return { code: source.replace(exportAnchor, `${observer}\n${exportAnchor}`), map: null }
  },
}

export default mergeConfig(baseConfig, {
  plugins: [ac21RouterObserver],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: backendUrl,
        changeOrigin: true,
      },
    },
  },
})
