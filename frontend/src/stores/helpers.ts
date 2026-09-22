import { ref, type Ref } from 'vue'
import { getApiErrorMessage } from '@/utils/error'

interface StoreActionOptions<T> {
  loadingRef: Ref<boolean>
  errorRef: Ref<string | null>
  onSuccess?: (data: T) => void
  onError?: (error: Error) => void
}

export async function storeAction<T>(
  fn: () => Promise<T>,
  options: StoreActionOptions<T>
): Promise<T> {
  const { loadingRef, errorRef, onSuccess, onError } = options

  loadingRef.value = true
  errorRef.value = null

  try {
    const result = await fn()
    onSuccess?.(result)
    return result
  } catch (e) {
    const errorMessage = e instanceof Error ? e.message : getApiErrorMessage(e)
    errorRef.value = errorMessage
    onError?.(e instanceof Error ? e : new Error(errorMessage))
    throw e
  } finally {
    loadingRef.value = false
  }
}

export function createLoadingState() {
  const loading = ref(false)
  const error = ref<string | null>(null)
  return { loading, error }
}
