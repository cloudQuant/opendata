import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import request from '@/utils/request'
import type {
  AuthResponse,
  CurrentAuthUser,
  LoginRequest,
  RefreshTokenResponse,
  RegisterRequest,
  RegisterResponse,
} from '@/types'

const messages = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}))

vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>('element-plus')
  return {
    ...actual,
    ElMessage: {
      success: messages.success,
      error: messages.error,
    },
  }
})

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface RequestCall {
  url: string
  method: string
  body: unknown
  authorization: string | undefined
}

const calls: RequestCall[] = []
let respond: (call: RequestCall) => WireEnvelope | Promise<WireEnvelope>
const originalAdapter = request.defaults.adapter
const originalLocalStorageDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage')

function createTestStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  }
}

function readBody(data: unknown): unknown {
  if (typeof data !== 'string') return data
  try {
    return JSON.parse(data) as unknown
  } catch {
    return data
  }
}

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig
): Promise<AxiosResponse> => {
  const call: RequestCall = {
    url: `${config.baseURL ?? ''}${config.url ?? ''}`,
    method: (config.method ?? 'get').toLowerCase(),
    body: readBody(config.data),
    authorization: config.headers?.Authorization as string | undefined,
  }
  calls.push(call)
  return {
    data: await respond(call),
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

const credentials: LoginRequest = {
  email: 'wire-user@example.com',
  password: 'strong-password-42',
}

const registerRequest: RegisterRequest = {
  email: 'new-wire-user@example.com',
  password: 'strong-password-43',
  password_confirm: 'strong-password-43',
}

const loginPayload: AuthResponse = {
  access_token: 'access.login.wire',
  refresh_token: 'refresh.login.wire',
  require_password_change: false,
  user: {
    user_id: 31,
    email: credentials.email,
    role: 'user',
    created_at: '2026-10-08T00:00:00Z',
    updated_at: null,
  },
}

const registerPayload: RegisterResponse = {
  user_id: 32,
  email: registerRequest.email,
  access_token: 'access.register.wire',
  refresh_token: 'refresh.register.wire',
}

const currentProfile: CurrentAuthUser = {
  user_id: registerPayload.user_id,
  email: registerPayload.email,
  role: 'user',
  is_active: true,
  created_at: '2026-10-08T00:00:00Z',
  updated_at: null,
}

const previousProfile: CurrentAuthUser = {
  user_id: 999,
  email: 'old-identity@example.test',
  role: 'admin',
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: null,
}

const refreshedPayload: RefreshTokenResponse = {
  access_token: 'access.refresh.wire',
  refresh_token: 'refresh.refresh.wire',
}

beforeEach(() => {
  calls.length = 0
  respond = (call) => {
    throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
  }
  request.defaults.adapter = adapter
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: createTestStorage(),
  })
  messages.success.mockClear()
  messages.error.mockClear()

  const pinia = createPinia()
  pinia.use(piniaPluginPersistedstate)
  setActivePinia(pinia)
})

afterEach(() => {
  request.defaults.adapter = originalAdapter
  if (originalLocalStorageDescriptor) {
    Object.defineProperty(window, 'localStorage', originalLocalStorageDescriptor)
  } else {
    Reflect.deleteProperty(window, 'localStorage')
  }
  vi.clearAllMocks()
})

describe('authentication API wire contracts', () => {
  it('returns the complete login payload and stores its user_id without synthesizing id', async () => {
    respond = () => ({ success: true, message: '登录成功', data: loginPayload })

    const apiResponse = await authApi.login(credentials)
    expect(apiResponse).toEqual(loginPayload)
    expect(apiResponse.require_password_change).toBe(false)
    expect(apiResponse.user.user_id).toBe(31)
    expect(apiResponse.user).not.toHaveProperty('id')
    expect(apiResponse.user).not.toHaveProperty('is_active')

    const store = useAuthStore()
    await store.login(credentials)

    expect(calls).toHaveLength(2)
    expect(calls.map(({ url, method, body }) => ({ url, method, body }))).toEqual([
      {
        url: '/api/v1/auth/login',
        method: 'post',
        body: credentials,
      },
      {
        url: '/api/v1/auth/login',
        method: 'post',
        body: credentials,
      },
    ])
    expect(store.user).toEqual(loginPayload.user)
    expect(store.user).not.toHaveProperty('id')
    expect(store.isAuthenticated).toBe(true)
  })

  it('uses registration tokens to fetch the current profile with Authorization', async () => {
    respond = (call) => {
      if (call.url === '/api/v1/auth/register') {
        return { success: true, message: '注册成功', data: registerPayload }
      }
      if (call.url === '/api/v1/auth/me') {
        return { success: true, data: currentProfile }
      }
      throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
    }

    const apiResponse = await authApi.register(registerRequest)
    expect(apiResponse).toEqual(registerPayload)
    expect(apiResponse).not.toHaveProperty('user')

    const store = useAuthStore()
    await store.register(registerRequest)

    expect(calls).toHaveLength(3)
    expect(calls[0]).toMatchObject({
      url: '/api/v1/auth/register',
      method: 'post',
      body: registerRequest,
      authorization: undefined,
    })
    expect(calls[1]).toMatchObject({
      url: '/api/v1/auth/register',
      method: 'post',
      body: registerRequest,
      authorization: undefined,
    })
    expect(calls[2]).toMatchObject({
      url: '/api/v1/auth/me',
      method: 'get',
      body: undefined,
      authorization: 'Bearer access.register.wire',
    })
    expect(store.accessToken).toBe(registerPayload.access_token)
    expect(store.refreshToken).toBe(registerPayload.refresh_token)
    expect(store.user).toEqual(currentProfile)
    expect(store.user?.user_id).toBe(32)
    expect(store.user).not.toHaveProperty('id')
  })

  it('clears the previous identity while the new registration profile is pending', async () => {
    let resolveProfileSeen!: () => void
    let resolveProfileResponse!: (envelope: WireEnvelope) => void
    const profileSeen = new Promise<void>((resolve) => {
      resolveProfileSeen = resolve
    })
    const profileResponse = new Promise<WireEnvelope>((resolve) => {
      resolveProfileResponse = resolve
    })
    respond = (call) => {
      if (call.url === '/api/v1/auth/register') {
        return { success: true, data: registerPayload }
      }
      if (call.url === '/api/v1/auth/me') {
        resolveProfileSeen()
        return profileResponse
      }
      throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
    }

    const store = useAuthStore()
    store.user = previousProfile
    store.accessToken = 'access.previous'
    store.refreshToken = 'refresh.previous'

    const pendingRegistration = store.register(registerRequest)
    await profileSeen

    expect(store.loading).toBe(true)
    expect(store.user).toBeNull()
    expect(store.isAuthenticated).toBe(false)
    expect(store.accessToken).toBe(registerPayload.access_token)
    expect(store.refreshToken).toBe(registerPayload.refresh_token)
    expect(calls).toHaveLength(2)
    expect(calls[1]).toMatchObject({
      url: '/api/v1/auth/me',
      method: 'get',
      authorization: 'Bearer access.register.wire',
    })
    expect(messages.success).not.toHaveBeenCalled()

    resolveProfileResponse({ success: true, data: currentProfile })
    await pendingRegistration

    expect(store.loading).toBe(false)
    expect(store.user).toEqual(currentProfile)
    expect(store.isAuthenticated).toBe(true)
    expect(messages.success).toHaveBeenCalledWith('注册成功')
  })

  it('refreshes tokens only and preserves the current profile', async () => {
    respond = () => ({ success: true, message: 'Token refreshed', data: refreshedPayload })
    const store = useAuthStore()
    store.user = currentProfile
    store.accessToken = 'access.before-refresh'
    store.refreshToken = 'refresh.before-refresh'

    const apiResponse = await authApi.refreshToken('refresh.before-refresh')
    expect(apiResponse).toEqual(refreshedPayload)
    expect(apiResponse).not.toHaveProperty('user')

    await store.refreshAccessToken()

    expect(calls).toHaveLength(2)
    expect(calls[0]).toMatchObject({
      url: '/api/v1/auth/refresh',
      method: 'post',
      body: { refresh_token: 'refresh.before-refresh' },
      authorization: 'Bearer access.before-refresh',
    })
    expect(calls[1]).toMatchObject(calls[0])
    expect(store.accessToken).toBe(refreshedPayload.access_token)
    expect(store.refreshToken).toBe(refreshedPayload.refresh_token)
    expect(store.user).toEqual(currentProfile)
  })

  it('reports login failure and clears loading without authenticating', async () => {
    respond = () => ({ success: false, message: '账号或密码错误' })
    const store = useAuthStore()

    await expect(store.login(credentials)).rejects.toThrow('账号或密码错误')

    expect(calls).toHaveLength(1)
    expect(store.loading).toBe(false)
    expect(store.error).toBe('账号或密码错误')
    expect(store.user).toBeNull()
    expect(store.accessToken).toBeNull()
    expect(store.isAuthenticated).toBe(false)
  })

  it('does not mark registration complete when /me fails after token issuance', async () => {
    respond = (call) => {
      if (call.url === '/api/v1/auth/register') {
        return { success: true, data: registerPayload }
      }
      if (call.url === '/api/v1/auth/me') {
        return { success: false, message: '用户资料暂不可用' }
      }
      throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
    }
    const store = useAuthStore()

    await expect(store.register(registerRequest)).rejects.toThrow('用户资料暂不可用')

    expect(calls.map(({ url }) => url)).toEqual(['/api/v1/auth/register', '/api/v1/auth/me'])
    expect(calls[1].authorization).toBe('Bearer access.register.wire')
    expect(store.loading).toBe(false)
    expect(store.error).toBe('用户资料暂不可用')
    expect(store.user).toBeNull()
    expect(store.accessToken).toBeNull()
    expect(store.refreshToken).toBeNull()
    expect(store.isAuthenticated).toBe(false)
  })

  it('clears a previous session and preserves the profile error when the new profile request fails', async () => {
    const profileError = new Error('新用户资料加载失败')
    respond = (call) => {
      if (call.url === '/api/v1/auth/register') {
        return { success: true, data: registerPayload }
      }
      if (call.url === '/api/v1/auth/me') return Promise.reject(profileError)
      throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
    }
    const store = useAuthStore()
    store.user = previousProfile
    store.accessToken = 'access.previous'
    store.refreshToken = 'refresh.previous'

    await expect(store.register(registerRequest)).rejects.toBe(profileError)

    expect(calls.map(({ url }) => url)).toEqual(['/api/v1/auth/register', '/api/v1/auth/me'])
    expect(calls[1].authorization).toBe('Bearer access.register.wire')
    expect(store.loading).toBe(false)
    expect(store.error).toBe(profileError.message)
    expect(store.user).toBeNull()
    expect(store.accessToken).toBeNull()
    expect(store.refreshToken).toBeNull()
    expect(store.isAuthenticated).toBe(false)
    expect(messages.error).toHaveBeenCalledWith(profileError.message)
    expect(messages.success).not.toHaveBeenCalled()
  })

  it('keeps the previous complete session if the registration request fails before issuing tokens', async () => {
    respond = (call) => {
      if (call.url === '/api/v1/auth/register') {
        return { success: false, message: '注册请求失败' }
      }
      throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
    }
    const store = useAuthStore()
    store.user = previousProfile
    store.accessToken = 'access.previous'
    store.refreshToken = 'refresh.previous'

    await expect(store.register(registerRequest)).rejects.toThrow('注册请求失败')

    expect(calls.map(({ url }) => url)).toEqual(['/api/v1/auth/register'])
    expect(store.loading).toBe(false)
    expect(store.error).toBe('注册请求失败')
    expect(store.user).toEqual(previousProfile)
    expect(store.accessToken).toBe('access.previous')
    expect(store.refreshToken).toBe('refresh.previous')
    expect(store.isAuthenticated).toBe(true)
    expect(messages.success).not.toHaveBeenCalled()
  })
})
