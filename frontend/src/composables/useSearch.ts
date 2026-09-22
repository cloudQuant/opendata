/**
 * Search composable for reusable search with debounce logic.
 *
 * Extracts common search state and debounced search functionality.
 */

import { ref, watch, type Ref } from 'vue'

export interface UseSearchOptions {
  /** Debounce delay in milliseconds, defaults to 300ms */
  debounceMs?: number
  /** Initial search keyword */
  initialKeyword?: string
  /** Callback when search is triggered */
  onSearch?: (keyword: string) => void | Promise<void>
}

export interface UseSearchReturn {
  /** Current search keyword input value */
  searchKeyword: Ref<string>
  /** Whether a search is in progress */
  isSearching: Ref<boolean>
  /** Trigger search immediately (bypass debounce) */
  triggerSearch: () => void
  /** Clear search keyword and trigger search */
  clearSearch: () => void
  /** Manual debounce reset (for cleanup) */
  cancelPendingSearch: () => void
}

/**
 * Reusable search logic with debounce for list views.
 *
 * @example
 * ```ts
 * const search = useSearch({
 *   debounceMs: 300,
 *   onSearch: async (keyword) => {
 *     await loadData({ keyword })
 *   }
 * })
 *
 * // In template
 * <el-input v-model="search.searchKeyword.value" @input="search.triggerSearch" />
 * ```
 */
export function useSearch(options: UseSearchOptions = {}): UseSearchReturn {
  const {
    debounceMs = 300,
    initialKeyword = '',
    onSearch
  } = options

  const searchKeyword = ref(initialKeyword)
  const isSearching = ref(false)

  let debounceTimer: ReturnType<typeof setTimeout> | null = null

  function cancelPendingSearch(): void {
    if (debounceTimer) {
      clearTimeout(debounceTimer)
      debounceTimer = null
    }
  }

  async function executeSearch(): Promise<void> {
    if (!onSearch) return

    cancelPendingSearch()
    isSearching.value = true
    try {
      await onSearch(searchKeyword.value)
    } finally {
      isSearching.value = false
    }
  }

  function triggerSearch(): void {
    cancelPendingSearch()

    if (debounceMs > 0) {
      debounceTimer = setTimeout(() => {
        void executeSearch()
      }, debounceMs)
    } else {
      void executeSearch()
    }
  }

  function clearSearch(): void {
    searchKeyword.value = ''
    void executeSearch()
  }

  return {
    searchKeyword,
    isSearching,
    triggerSearch,
    clearSearch,
    cancelPendingSearch
  }
}

/**
 * Creates a debounced function that delays invoking `fn` until after `delay` milliseconds
 * have elapsed since the last time the debounced function was invoked.
 *
 * @example
 * ```ts
 * const debouncedSearch = useDebounceFn((keyword: string) => {
 *   loadData({ keyword })
 * }, 300)
 *
 * // Call repeatedly - only executes after 300ms of inactivity
 * debouncedSearch('test')
 * debouncedSearch('testing') // Cancels previous, restarts timer
 * ```
 */
export function useDebounceFn<T extends (...args: unknown[]) => unknown>(
  fn: T,
  delay: number
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null

  return (...args: Parameters<T>) => {
    if (timer) {
      clearTimeout(timer)
    }
    timer = setTimeout(() => {
      fn(...args)
      timer = null
    }, delay)
  }
}
