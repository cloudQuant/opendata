import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Execution, ExecutionStats, PaginationParams, PaginatedResponse } from '@/types'
import { dataApi } from '@/api/data'
import { useStoreAction, useStoreList } from '@/composables/useStoreAction'

export const useExecutionStore = defineStore(
  'executions',
  () => {
    const listHelper = useStoreList<Execution>({ defaultPageSize: 20 })
    const actionHelper = useStoreAction()

    const currentExecution = ref<Execution | null>(null)
    const stats = ref<ExecutionStats | null>(null)
    const runningExecutions = ref<Execution[]>([])

    const filters = ref<{
      script_id?: number
      status?: string
    }>({})

    const hasMore = computed(() => listHelper.items.value.length < listHelper.total.value)
    const pendingCount = computed(
      () => listHelper.items.value.filter((e) => e.status === 'pending').length
    )
    const runningCount = computed(
      () => listHelper.items.value.filter((e) => e.status === 'running').length
    )
    const failedCount = computed(
      () => listHelper.items.value.filter((e) => e.status === 'failed').length
    )
    const completedCount = computed(
      () => listHelper.items.value.filter((e) => e.status === 'completed').length
    )

    async function fetchExecutions(params?: PaginationParams & { script_id?: number; status?: string }) {
      await listHelper.load(
        async ({ page, pageSize }) => {
          const response: PaginatedResponse<Execution> = await dataApi.listExecutions({
            page,
            page_size: pageSize,
            ...filters.value,
            ...params,
          })
          return {
            items: response.items,
            total: response.total,
          }
        },
        { errorMessage: '获取执行记录失败' }
      )
      listHelper.page.value = params?.page ?? listHelper.page.value
    }

    async function fetchExecution(executionId: number) {
      await actionHelper.execute(
        async () => {
          currentExecution.value = await dataApi.getExecution(executionId)
          return currentExecution.value
        },
        { errorMessage: '获取执行详情失败' }
      )
    }

    async function fetchStats(params?: { start_date?: string; end_date?: string }) {
      await actionHelper.execute(
        async () => {
          stats.value = await dataApi.getStats(params)
          return stats.value
        },
        { errorMessage: '获取统计数据失败' }
      )
    }

    async function fetchRecentExecutions(limit: number = 50) {
      await actionHelper.execute(
        async () => {
          const recent = await dataApi.getRecent(limit)
          const existingIds = new Set(listHelper.items.value.map((e) => e.id))
          const newExecutions = recent.filter((e) => !existingIds.has(e.id))
          listHelper.items.value = [...newExecutions, ...listHelper.items.value].slice(0, 100)
          return listHelper.items.value
        },
        { errorMessage: '获取最近执行记录失败' }
      )
    }

    async function fetchRunningExecutions() {
      await actionHelper.execute(
        async () => {
          runningExecutions.value = await dataApi.getRunning()
          return runningExecutions.value
        },
        { errorMessage: '获取运行中任务失败' }
      )
    }

    async function retryExecution(executionId: number) {
      await actionHelper.execute(
        async () => {
          await dataApi.retry(executionId)
          await fetchExecutions()
        },
        { errorMessage: '重试执行失败' }
      )
    }

    function setFilters(newFilters: { script_id?: number; status?: string }) {
      filters.value = { ...filters.value, ...newFilters }
      listHelper.page.value = 1
    }

    function clearFilters() {
      filters.value = {}
      listHelper.page.value = 1
    }

    function reset() {
      listHelper.reset()
      currentExecution.value = null
      stats.value = null
      runningExecutions.value = []
      filters.value = {}
    }

    return {
      executions: listHelper.items,
      currentExecution,
      stats,
      runningExecutions,
      total: listHelper.total,
      loading: computed(() => listHelper.loading.value || actionHelper.loading.value),
      error: computed(() => listHelper.error.value || actionHelper.error.value),
      page: listHelper.page,
      pageSize: listHelper.pageSize,
      filters,
      hasMore,
      pendingCount,
      runningCount,
      failedCount,
      completedCount,
      fetchExecutions,
      fetchExecution,
      fetchStats,
      fetchRecentExecutions,
      fetchRunningExecutions,
      retryExecution,
      setFilters,
      clearFilters,
      setPage: listHelper.setPage,
      setPageSize: listHelper.setPageSize,
      reset,
    }
  },
  {
    persist: {
      paths: ['page', 'pageSize', 'filters'],
    },
  }
)
