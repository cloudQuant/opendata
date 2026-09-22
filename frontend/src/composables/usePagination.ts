/**
 * Pagination composable for reusable pagination logic.
 *
 * Extracts common pagination state and handlers used across list views.
 */

import { ref, computed, type Ref, type ComputedRef } from 'vue'
import { PAGINATION } from '@/config/constants'

export interface UsePaginationOptions {
  /** Default page size, defaults to config value */
  defaultPageSize?: number
  /** Maximum page size allowed */
  maxPageSize?: number
  /** Total number of items */
  total?: Ref<number> | number
}

export interface UsePaginationReturn {
  /** Current page number (1-indexed) */
  currentPage: Ref<number>
  /** Items per page */
  pageSize: Ref<number>
  /** Total items count */
  totalItems: Ref<number>
  /** Calculated offset for API calls */
  offset: ComputedRef<number>
  /** Total pages calculated from total and pageSize */
  totalPages: ComputedRef<number>
  /** Handle page change event from pagination component */
  handlePageChange: (page: number) => void
  /** Handle page size change event */
  handleSizeChange: (size: number) => void
  /** Reset to first page */
  resetPage: () => void
  /** Set total items count */
  setTotal: (total: number) => void
  /** Get pagination params for API calls */
  getPaginationParams: () => { page: number; page_size: number }
}

/**
 * Reusable pagination logic for list views.
 *
 * @example
 * ```ts
 * const pagination = usePagination({ defaultPageSize: 20 })
 *
 * // In API call
 * const data = await api.list({
 *   ...pagination.getPaginationParams(),
 *   keyword: searchKeyword.value
 * })
 *
 * // Update total
 * pagination.setTotal(data.total)
 * ```
 */
export function usePagination(options: UsePaginationOptions = {}): UsePaginationReturn {
  const {
    defaultPageSize = PAGINATION.DEFAULT_PAGE_SIZE,
    maxPageSize = PAGINATION.MAX_PAGE_SIZE,
    total: initialTotal = 0
  } = options

  const currentPage = ref(1)
  const pageSize = ref(Math.min(defaultPageSize, maxPageSize))
  const totalItems = ref(typeof initialTotal === 'number' ? initialTotal : 0)

  const offset = computed(() => (currentPage.value - 1) * pageSize.value)

  const totalPages = computed(() => Math.ceil(totalItems.value / pageSize.value) || 1)

  function handlePageChange(page: number): void {
    currentPage.value = page
  }

  function handleSizeChange(size: number): void {
    pageSize.value = Math.min(size, maxPageSize)
    currentPage.value = 1
  }

  function resetPage(): void {
    currentPage.value = 1
  }

  function setTotal(total: number): void {
    totalItems.value = total
    if (currentPage.value > totalPages.value) {
      currentPage.value = Math.max(1, totalPages.value)
    }
  }

  function getPaginationParams(): { page: number; page_size: number } {
    return {
      page: currentPage.value,
      page_size: pageSize.value
    }
  }

  return {
    currentPage,
    pageSize,
    totalItems,
    offset,
    totalPages,
    handlePageChange,
    handleSizeChange,
    resetPage,
    setTotal,
    getPaginationParams
  }
}
