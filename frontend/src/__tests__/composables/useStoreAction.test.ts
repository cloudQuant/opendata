import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useStoreAction, useStoreList } from '@/composables/useStoreAction'
import { ElMessage } from 'element-plus'

vi.mock('element-plus', () => ({
  ElMessage: {
    error: vi.fn(),
    success: vi.fn(),
  },
}))

describe('useStoreAction', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('execute', () => {
    it('should return result on success', async () => {
      const { execute, loading, error } = useStoreAction()

      const result = await execute(async () => 'success')

      expect(result).toBe('success')
      expect(loading.value).toBe(false)
      expect(error.value).toBeNull()
    })

    it('should set loading state during execution', async () => {
      const { execute, loading } = useStoreAction()

      const promise = execute(
        () => new Promise((resolve) => setTimeout(() => resolve('done'), 10))
      )

      expect(loading.value).toBe(true)

      await promise

      expect(loading.value).toBe(false)
    })

    it('should handle errors and show toast', async () => {
      const { execute, error } = useStoreAction()

      await expect(
        execute(async () => {
          throw new Error('Test error')
        })
      ).rejects.toThrow('Test error')

      expect(error.value).toBe('Test error')
      expect(ElMessage.error).toHaveBeenCalledWith('Test error')
    })

    it('should use custom error message', async () => {
      const { execute, error } = useStoreAction()

      await expect(
        execute(async () => {
          throw new Error('Original error')
        }, { errorMessage: 'Custom error' })
      ).rejects.toThrow()

      expect(error.value).toBe('Original error')
    })

    it('should not show toast when showErrorToast is false', async () => {
      const { execute } = useStoreAction()

      await expect(
        execute(
          async () => {
            throw new Error('Silent error')
          },
          { showErrorToast: false }
        )
      ).rejects.toThrow()

      expect(ElMessage.error).not.toHaveBeenCalled()
    })

    it('should call onSuccess callback', async () => {
      const { execute } = useStoreAction()
      const onSuccess = vi.fn()

      await execute(async () => 'result', { onSuccess })

      expect(onSuccess).toHaveBeenCalled()
    })

    it('should call onError callback', async () => {
      const { execute } = useStoreAction()
      const onError = vi.fn()

      await expect(
        execute(
          async () => {
            throw new Error('Test error')
          },
          { onError }
        )
      ).rejects.toThrow()

      expect(onError).toHaveBeenCalledWith(expect.any(Error))
    })
  })

  describe('reset', () => {
    it('should reset loading and error states', async () => {
      const { execute, reset, loading, error } = useStoreAction()

      await expect(
        execute(async () => {
          throw new Error('Test error')
        })
      ).rejects.toThrow()

      expect(error.value).toBe('Test error')

      reset()

      expect(loading.value).toBe(false)
      expect(error.value).toBeNull()
    })
  })

  describe('setError', () => {
    it('should set error message', () => {
      const { setError, error } = useStoreAction()

      setError('Custom error')

      expect(error.value).toBe('Custom error')
    })
  })
})

describe('useStoreList', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('load', () => {
    it('should load items and set total', async () => {
      const { load, items, total, loading } = useStoreList<string>()

      await load(async () => ({
        items: ['item1', 'item2', 'item3'],
        total: 3,
      }))

      expect(items.value).toEqual(['item1', 'item2', 'item3'])
      expect(total.value).toBe(3)
      expect(loading.value).toBe(false)
    })

    it('should pass page and pageSize to load function', async () => {
      const { load, page, pageSize } = useStoreList({ defaultPageSize: 50 })

      page.value = 2
      pageSize.value = 30

      let receivedParams: { page: number; pageSize: number } | null = null

      await load(async (params) => {
        receivedParams = params
        return { items: [], total: 0 }
      })

      expect(receivedParams).toEqual({ page: 2, pageSize: 30 })
    })

    it('should handle load errors', async () => {
      const { load, error } = useStoreList<string>()

      await expect(
        load(async () => {
          throw new Error('Load failed')
        })
      ).rejects.toThrow()

      expect(error.value).toBe('Load failed')
      expect(ElMessage.error).toHaveBeenCalledWith('Load failed')
    })

    it('should use custom error message', async () => {
      const { load, error } = useStoreList<string>()

      await expect(
        load(
          async () => {
            throw new Error('Original error')
          },
          { errorMessage: 'Custom load error' }
        )
      ).rejects.toThrow()

      expect(error.value).toBe('Original error')
    })
  })

  describe('setPage', () => {
    it('should update page value', () => {
      const { setPage, page } = useStoreList()

      setPage(5)

      expect(page.value).toBe(5)
    })
  })

  describe('setPageSize', () => {
    it('should update pageSize and reset page to 1', () => {
      const { setPageSize, pageSize, page } = useStoreList()

      page.value = 5
      setPageSize(50)

      expect(pageSize.value).toBe(50)
      expect(page.value).toBe(1)
    })
  })

  describe('reset', () => {
    it('should reset all state to defaults', async () => {
      const { load, reset, items, total, page, pageSize, loading, error } = useStoreList<string>()

      await load(async () => ({
        items: ['item1'],
        total: 1,
      }))

      page.value = 5

      reset()

      expect(items.value).toEqual([])
      expect(total.value).toBe(0)
      expect(page.value).toBe(1)
      expect(pageSize.value).toBe(20)
      expect(loading.value).toBe(false)
      expect(error.value).toBeNull()
    })

    it('should use custom default page size', () => {
      const { reset, pageSize } = useStoreList({ defaultPageSize: 50 })

      pageSize.value = 100
      reset()

      expect(pageSize.value).toBe(50)
    })
  })
})
