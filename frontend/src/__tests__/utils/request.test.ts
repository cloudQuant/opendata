import { describe, it, expect, vi, beforeAll, afterAll, beforeEach } from 'vitest'
import { AxiosError, type AxiosAdapter, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'

const authStoreMocks = vi.hoisted(() => ({
  logout: vi.fn(),
  refreshAccessToken: vi.fn(),
}))
let authRefreshToken: string | null = 'test-refresh'

// Mock pinia store before importing request
vi.mock('@/stores/auth', () => ({
  useAuthStore: vi.fn(() => ({
    accessToken: 'test-token',
    refreshToken: authRefreshToken,
    refreshAccessToken: authStoreMocks.refreshAccessToken,
    logout: authStoreMocks.logout,
  })),
}))

// Mock element-plus
vi.mock('element-plus', () => ({
  ElMessage: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}))

// The contract under test is what a caller of `request` receives, so the
// transport is stubbed and the real instance with its real interceptors runs.
// (An earlier version of this file only asserted that `interceptors.request`
// was defined, which is true of any axios instance and of none of the
// behaviour the api modules depend on.)
const bodies = new Map<string, unknown>()
let responseStatus: number | null = null

let lastConfig: InternalAxiosRequestConfig | undefined

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig,
): Promise<AxiosResponse> => {
  lastConfig = config
  if (responseStatus !== null) {
    const response = {
      data: { detail: 'Invalid email or password' },
      status: responseStatus,
      statusText: 'Unauthorized',
      headers: {},
      config,
      request: {},
    } as AxiosResponse
    throw new AxiosError(
      `Request failed with status code ${responseStatus}`,
      AxiosError.ERR_BAD_REQUEST,
      config,
      {},
      response
    )
  }
  return {
    data: bodies.get(config.url ?? ''),
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

const { default: request } = await import('@/utils/request')
const { ElMessage } = await import('element-plus')

const originalAdapter = request.defaults.adapter

beforeAll(() => {
  request.defaults.adapter = adapter
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
})

beforeEach(() => {
  vi.clearAllMocks()
  bodies.clear()
  lastConfig = undefined
  responseStatus = null
  authRefreshToken = 'test-refresh'
  authStoreMocks.logout.mockReset()
  authStoreMocks.refreshAccessToken.mockReset()
})

describe('Request Utils', () => {
  it('creates an axios instance', async () => {
    expect(request).toBeDefined()
    expect(request.defaults.baseURL).toBe('/api/v1')
    expect(request.defaults.timeout).toBe(30000)
  })

  it('sends the bearer token on every request', async () => {
    bodies.set('/whoami', { ok: true })

    await request.get('/whoami')

    expect(lastConfig?.headers?.Authorization).toBe('Bearer test-token')
  })

  it('hands the caller the payload inside the envelope', async () => {
    bodies.set('/data/catalog', {
      success: true,
      message: 'success',
      data: { domains: [{ domain: 'stock_daily' }] },
    })

    const payload = await request.get('/data/catalog')

    expect(payload).toEqual({ domains: [{ domain: 'stock_daily' }] })
  })

  it('hands the caller a body that carries no envelope at all', async () => {
    // Measured: /settings/database and /tables/{id}/schema return the model
    // directly, which is why the api callers must not unwrap a second time.
    bodies.set('/settings/database', { host: '127.0.0.1', port: 3306, database: 'opendata' })

    const payload = await request.get('/settings/database')

    expect(payload).toEqual({ host: '127.0.0.1', port: 3306, database: 'opendata' })
  })

  it('rejects a 200 whose envelope reports failure', async () => {
    // Reachable: opendata/api/settings.py returns success=false with HTTP 200.
    bodies.set('/settings/database/test', { success: false, message: '连接失败' })

    await expect(request.post('/settings/database/test')).rejects.toThrow('连接失败')
    expect(ElMessage.error).toHaveBeenCalledWith('连接失败')
  })

  it('leaves an exact login 401 to the caller without expiring the session', async () => {
    responseStatus = 401

    await expect(request.post('/auth/login', { email: 'bad@example.com', password: 'wrong' }))
      .rejects.toThrow('Request failed with status code 401')

    expect(authStoreMocks.logout).not.toHaveBeenCalled()
    expect(authStoreMocks.refreshAccessToken).not.toHaveBeenCalled()
    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('expires a protected request session and redirects with the fixed reason', async () => {
    responseStatus = 401
    authRefreshToken = null
    const fakeWindow = { location: { origin: 'http://localhost', href: '/' } }
    vi.stubGlobal('window', fakeWindow)
    try {
      await expect(request.post('/data/download', { interface_id: 7 })).rejects.toThrow(
        'Request failed with status code 401'
      )

      expect(authStoreMocks.logout).toHaveBeenCalledOnce()
      expect(fakeWindow.location.href).toBe('/login?reason=session_expired')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
