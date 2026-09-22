import { ref, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getApiErrorMessage } from '@/utils/error'

export interface UseApiCallOptions<T> {
  onSuccess?: (data: T) => void
  onError?: (error: Error) => void
  showErrorToast?: boolean
}

export interface UseApiCallReturn<T> {
  data: Ref<T | null>
  loading: Ref<boolean>
  error: Ref<string | null>
  execute: (fn: () => Promise<T>, options?: UseApiCallOptions<T>) => Promise<T | null>
  reset: () => void
}

export function useApiCall<T>(): UseApiCallReturn<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function execute(
    fn: () => Promise<T>,
    options: UseApiCallOptions<T> = {}
  ): Promise<T | null> {
    const { onSuccess, onError, showErrorToast = true } = options

    loading.value = true
    error.value = null

    try {
      const result = await fn()
      data.value = result
      onSuccess?.(result)
      return result
    } catch (e) {
      const errorMessage = e instanceof Error ? e.message : getApiErrorMessage(e)
      error.value = errorMessage

      if (showErrorToast) {
        ElMessage.error(errorMessage)
      }

      onError?.(e instanceof Error ? e : new Error(errorMessage))
      return null
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    data.value = null
    loading.value = false
    error.value = null
  }

  return {
    data,
    loading,
    error,
    execute,
    reset,
  }
}

export function useApiList<T>(): {
  items: Ref<T[]>
  total: Ref<number>
  loading: Ref<boolean>
  error: Ref<string | null>
  load: (fn: () => Promise<{ items: T[]; total: number }>) => Promise<void>
  reset: () => void
} {
  const items = ref<T[]>([]) as Ref<T[]>
  const total = ref(0)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function load(fn: () => Promise<{ items: T[]; total: number }>): Promise<void> {
    loading.value = true
    error.value = null

    try {
      const result = await fn()
      items.value = result.items
      total.value = result.total
    } catch (e) {
      const errorMessage = e instanceof Error ? e.message : getApiErrorMessage(e)
      error.value = errorMessage
      ElMessage.error(errorMessage)
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    items.value = []
    total.value = 0
    loading.value = false
    error.value = null
  }

  return {
    items,
    total,
    loading,
    error,
    load,
    reset,
  }
}
