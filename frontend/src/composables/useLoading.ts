import { ref, readonly } from 'vue'

const globalLoading = ref(false)
const loadingText = ref('')

export function useLoading() {
  function showLoading(text = '') {
    globalLoading.value = true
    loadingText.value = text
  }

  function hideLoading() {
    globalLoading.value = false
    loadingText.value = ''
  }

  async function withLoading<T>(fn: () => Promise<T>, text = ''): Promise<T> {
    showLoading(text)
    try {
      return await fn()
    } finally {
      hideLoading()
    }
  }

  return {
    globalLoading: readonly(globalLoading),
    loadingText: readonly(loadingText),
    showLoading,
    hideLoading,
    withLoading,
  }
}
