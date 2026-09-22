import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock auth store
vi.mock('@/stores/auth', () => ({
  useAuthStore: vi.fn(() => ({
    isAuthenticated: false,
    isAdmin: false,
    accessToken: null,
    refreshToken: null,
  })),
}))

describe('Router', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('exports a router instance', async () => {
    const { default: router } = await import('@/router/index')
    expect(router).toBeDefined()
    expect(router.getRoutes).toBeDefined()
  })

  it('has login route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const loginRoute = routes.find(r => r.name === 'Login')
    expect(loginRoute).toBeDefined()
    expect(loginRoute?.path).toBe('/login')
  })

  it('has register route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const registerRoute = routes.find(r => r.name === 'Register')
    expect(registerRoute).toBeDefined()
    expect(registerRoute?.path).toBe('/register')
  })

  it('has home route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const homeRoute = routes.find(r => r.name === 'Home')
    expect(homeRoute).toBeDefined()
  })

  it('has scripts route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const scriptsRoute = routes.find(r => r.name === 'Scripts')
    expect(scriptsRoute).toBeDefined()
  })

  it('has tasks route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const tasksRoute = routes.find(r => r.name === 'Tasks')
    expect(tasksRoute).toBeDefined()
  })

  it('has tables route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const tablesRoute = routes.find(r => r.name === 'Tables')
    expect(tablesRoute).toBeDefined()
  })

  it('has users route (admin)', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const usersRoute = routes.find(r => r.name === 'Users')
    expect(usersRoute).toBeDefined()
    expect(usersRoute?.meta?.requiresAdmin).toBe(true)
  })

  it('has settings route (admin)', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const settingsRoute = routes.find(r => r.name === 'Settings')
    expect(settingsRoute).toBeDefined()
    expect(settingsRoute?.meta?.requiresAdmin).toBe(true)
  })

  it('has 404 not found route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const notFoundRoute = routes.find(r => r.name === 'NotFound')
    expect(notFoundRoute).toBeDefined()
  })

  it('has executions route', async () => {
    const { default: router } = await import('@/router/index')
    const routes = router.getRoutes()
    const executionsRoute = routes.find(r => r.name === 'Executions')
    expect(executionsRoute).toBeDefined()
  })
})
