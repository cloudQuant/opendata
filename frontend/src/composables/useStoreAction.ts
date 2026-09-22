import { ref, type Ref } from 'vue'
import { ElMessage } from 'element-plus'

export interface StoreActionOptions {
  errorMessage?: string
  showErrorToast?: boolean
  onSuccess?: () => void
  onError?: (error: Error) => void
}

export interface UseStoreActionReturn {
  loading: Ref<boolean>
  error: Ref<string | null>
  execute: <T>(action: () => Promise<T>, options?: StoreActionOptions) => Promise<T | null>
  reset: () => void
  setError: (message: string | null) => void
}

export function useStoreAction(): UseStoreActionReturn {
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function execute<T>(
    action: () => Promise<T>,
    options: StoreActionOptions = {}
  ): Promise<T | null> {
    const { errorMessage, showErrorToast = true, onSuccess, onError } = options

    loading.value = true
    error.value = null

    try {
      const result = await action()
      onSuccess?.()
      return result
    } catch (e) {
      const message = e instanceof Error ? e.message : errorMessage || '操作失败'
      error.value = message

      if (showErrorToast) {
        ElMessage.error(message)
      }

      onError?.(e instanceof Error ? e : new Error(message))
      throw e
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    loading.value = false
    error.value = null
  }

  function setError(message: string | null): void {
    error.value = message
  }

  return { loading, error, execute, reset, setError }
}

export interface UseStoreListOptions {
  defaultPageSize?: number
}

export interface UseStoreListReturn<T> {
  items: Ref<T[]>
  total: Ref<number>
  page: Ref<number>
  pageSize: Ref<number>
  loading: Ref<boolean>
  error: Ref<string | null>
  load: (
    fn: (params: { page: number; pageSize: number }) => Promise<{ items: T[]; total: number }>,
    options?: { errorMessage?: string; showErrorToast?: boolean }
  ) => Promise<void>
  reset: () => void
  setPage: (page: number) => void
  setPageSize: (pageSize: number) => void
}

export function useStoreList<T>(options: UseStoreListOptions = {}): UseStoreListReturn<T> {
  const { defaultPageSize = 20 } = options

  const items = ref<T[]>([]) as Ref<T[]>
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(defaultPageSize)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function load(
    fn: (params: { page: number; pageSize: number }) => Promise<{ items: T[]; total: number }>,
    loadOptions: { errorMessage?: string; showErrorToast?: boolean } = {}
  ): Promise<void> {
    const { errorMessage, showErrorToast = true } = loadOptions

    loading.value = true
    error.value = null

    try {
      const result = await fn({ page: page.value, pageSize: pageSize.value })
      items.value = result.items
      total.value = result.total
    } catch (e) {
      const message = e instanceof Error ? e.message : errorMessage || '获取列表失败'
      error.value = message

      if (showErrorToast) {
        ElMessage.error(message)
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    items.value = []
    total.value = 0
    page.value = 1
    pageSize.value = defaultPageSize
    loading.value = false
    error.value = null
  }

  function setPage(newPage: number): void {
    page.value = newPage
  }

  function setPageSize(newPageSize: number): void {
    pageSize.value = newPageSize
    page.value = 1
  }

  return { items, total, page, pageSize, loading, error, load, reset, setPage, setPageSize }
}
