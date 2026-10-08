import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'
import { useAuthStore } from '@/stores/auth'
import type {
  AuthResponse,
  AuthUser,
  CurrentAuthUser,
  RefreshTokenResponse,
  RegisterResponse,
} from '@/types'

// Mock the auth API
vi.mock('@/api/auth', () => ({
  authApi: {
    login: vi.fn(),
    register: vi.fn(),
    refreshToken: vi.fn(),
    logout: vi.fn(),
    getCurrentUser: vi.fn(),
  },
}))

// Mock localStorage
const localStorageMock = (() => {
  let store: Record<string, string> = {}
  return {
    getItem: vi.fn((key: string) => store[key] || null),
    setItem: vi.fn((key: string, value: string) => { store[key] = value }),
    removeItem: vi.fn((key: string) => { delete store[key] }),
    clear: vi.fn(() => { store = {} }),
  }
})()
Object.defineProperty(window, 'localStorage', { value: localStorageMock })

function makeAuthUser(role: 'admin' | 'user' = 'user', user_id = 1): AuthUser {
  return {
    user_id,
    email: `${role}@example.com`,
    role,
    created_at: '2026-10-08T00:00:00Z',
    updated_at: null,
  }
}

function makeCurrentAuthUser(role: 'admin' | 'user' = 'user', user_id = 1): CurrentAuthUser {
  return {
    ...makeAuthUser(role, user_id),
    is_active: true,
  }
}

describe('Auth Store', () => {
  beforeEach(() => {
    const pinia = createPinia()
    pinia.use(piniaPluginPersistedstate)
    setActivePinia(pinia)
    localStorageMock.clear()
    vi.clearAllMocks()
  })

  it('initializes with null values', () => {
    const store = useAuthStore()
    expect(store.user).toBeNull()
    expect(store.accessToken).toBeNull()
    expect(store.refreshToken).toBeNull()
  })

  it('isAuthenticated returns false when no user/token', () => {
    const store = useAuthStore()
    expect(store.isAuthenticated).toBe(false)
  })

  it('isAuthenticated returns true when user and token exist', () => {
    const store = useAuthStore()
    store.user = makeCurrentAuthUser()
    store.accessToken = 'token123'
    expect(store.isAuthenticated).toBe(true)
  })

  it('isAdmin returns false for regular user', () => {
    const store = useAuthStore()
    store.user = makeAuthUser('user')
    expect(store.isAdmin).toBe(false)
  })

  it('isAdmin returns true for admin user', () => {
    const store = useAuthStore()
    store.user = makeAuthUser('admin')
    expect(store.isAdmin).toBe(true)
  })

  it('login sets tokens and user', async () => {
    const { authApi } = await import('@/api/auth')
    const mockResponse: AuthResponse = {
      access_token: 'access123',
      refresh_token: 'refresh123',
      require_password_change: false,
      user: makeAuthUser('user', 1),
    }
    vi.mocked(authApi.login).mockResolvedValue(mockResponse)

    const store = useAuthStore()
    await store.login({ email: 'test@example.com', password: 'pass' })

    expect(store.accessToken).toBe('access123')
    expect(store.refreshToken).toBe('refresh123')
    expect(store.user).toEqual(mockResponse.user)
  })

  it('register sets tokens and user', async () => {
    const { authApi } = await import('@/api/auth')
    const mockResponse: RegisterResponse = {
      user_id: 2,
      email: 'new@example.com',
      access_token: 'access456',
      refresh_token: 'refresh456',
    }
    const profile = makeCurrentAuthUser('user', 2)
    vi.mocked(authApi.register).mockResolvedValue(mockResponse)
    vi.mocked(authApi.getCurrentUser).mockResolvedValue(profile)

    const store = useAuthStore()
    await store.register({ email: 'new@example.com', password: 'pass', password_confirm: 'pass' })

    expect(store.accessToken).toBe('access456')
    expect(store.refreshToken).toBe('refresh456')
    expect(authApi.getCurrentUser).toHaveBeenCalledOnce()
    expect(store.user).toEqual(profile)
  })

  it('logout clears state', async () => {
    const store = useAuthStore()
    store.user = makeAuthUser()
    store.accessToken = 'token'
    store.refreshToken = 'refresh'

    await store.logout()

    expect(store.user).toBeNull()
    expect(store.accessToken).toBeNull()
    expect(store.refreshToken).toBeNull()
  })

  it('setUser updates user', () => {
    const store = useAuthStore()
    const user = makeAuthUser('admin', 4)
    store.setUser(user)
    expect(store.user).toEqual(user)
  })

  it('refreshAccessToken throws when no refresh token', async () => {
    const store = useAuthStore()
    store.refreshToken = null

    await expect(store.refreshAccessToken()).rejects.toThrow('No refresh token available')
  })

  it('refreshAccessToken updates tokens', async () => {
    const { authApi } = await import('@/api/auth')
    const response: RefreshTokenResponse = {
      access_token: 'new_access',
      refresh_token: 'new_refresh',
    }
    vi.mocked(authApi.refreshToken).mockResolvedValue(response)

    const store = useAuthStore()
    store.refreshToken = 'old_refresh'
    const originalUser = makeCurrentAuthUser('admin', 7)
    store.user = originalUser

    await store.refreshAccessToken()

    expect(store.accessToken).toBe('new_access')
    expect(store.refreshToken).toBe('new_refresh')
    expect(store.user).toEqual(originalUser)
  })
})
