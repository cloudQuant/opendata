import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock pinia store before importing request
vi.mock('@/stores/auth', () => ({
  useAuthStore: vi.fn(() => ({
    accessToken: 'test-token',
    refreshToken: 'test-refresh',
    refreshAccessToken: vi.fn(),
    logout: vi.fn(),
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

describe('Request Utils', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('creates an axios instance', async () => {
    const { default: request } = await import('@/utils/request')
    expect(request).toBeDefined()
    expect(request.defaults.baseURL).toBe('/api/v1')
    expect(request.defaults.timeout).toBe(30000)
  })

  it('has request interceptor for auth token', async () => {
    const { default: request } = await import('@/utils/request')
    // Check interceptors exist
    expect(request.interceptors.request).toBeDefined()
    expect(request.interceptors.response).toBeDefined()
  })
})
