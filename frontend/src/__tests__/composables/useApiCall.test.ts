import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useApiCall } from '@/composables/useApiCall'

// Mock element-plus
vi.mock('element-plus', () => ({
  ElMessage: {
    error: vi.fn(),
    success: vi.fn(),
  },
}))

// Mock error utility
vi.mock('@/utils/error', () => ({
  getApiErrorMessage: vi.fn((error: unknown) => {
    if (error instanceof Error) return error.message
    return 'Unknown error'
  }),
}))

describe('useApiCall', () => {
  it('should initialize with default values', () => {
    const { data, loading, error } = useApiCall<string>()

    expect(data.value).toBeNull()
    expect(loading.value).toBe(false)
    expect(error.value).toBeNull()
  })

  it('should handle successful API call', async () => {
    const { data, loading, error, execute } = useApiCall<string>()
    const mockData = 'test result'

    const result = await execute(() => Promise.resolve(mockData))

    expect(result).toBe(mockData)
    expect(data.value).toBe(mockData)
    expect(loading.value).toBe(false)
    expect(error.value).toBeNull()
  })

  it('should handle failed API call', async () => {
    const { data, loading, error, execute } = useApiCall<string>()
    const mockError = new Error('API Error')

    const result = await execute(() => Promise.reject(mockError))

    expect(result).toBeNull()
    expect(data.value).toBeNull()
    expect(loading.value).toBe(false)
    expect(error.value).toBe('API Error')
  })

  it('should show error toast by default', async () => {
    const { ElMessage } = await import('element-plus')
    const { execute } = useApiCall<string>()
    const mockError = new Error('Test error')

    await execute(() => Promise.reject(mockError))

    expect(ElMessage.error).toHaveBeenCalledWith('Test error')
  })

  it('should hide error toast when showErrorToast is false', async () => {
    const { ElMessage } = await import('element-plus')
    vi.mocked(ElMessage.error).mockClear()
    const { execute } = useApiCall<string>()
    const mockError = new Error('Test error')

    await execute(() => Promise.reject(mockError), { showErrorToast: false })

    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('should call onSuccess callback', async () => {
    const { execute } = useApiCall<string>()
    const mockData = 'success data'
    const onSuccess = vi.fn()

    await execute(() => Promise.resolve(mockData), { onSuccess })

    expect(onSuccess).toHaveBeenCalledWith(mockData)
  })

  it('should call onError callback', async () => {
    const { execute } = useApiCall<string>()
    const mockError = new Error('custom error')
    const onError = vi.fn()

    await execute(() => Promise.reject(mockError), { onError, showErrorToast: false })

    expect(onError).toHaveBeenCalledWith(mockError)
  })

  it('should reset state', async () => {
    const { data, loading, error, execute, reset } = useApiCall<string>()

    await execute(() => Promise.resolve('test'))
    reset()

    expect(data.value).toBeNull()
    expect(error.value).toBeNull()
  })

  it('should manage loading state correctly', async () => {
    const { loading, execute } = useApiCall<string>()
    let resolveFn: (value: string) => void

    const promise = new Promise<string>((resolve) => {
      resolveFn = resolve
    })

    const executePromise = execute(() => promise)

    expect(loading.value).toBe(true)

    resolveFn('done')
    await executePromise

    expect(loading.value).toBe(false)
  })
})
