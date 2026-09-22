import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Task, PaginationParams, PaginatedResponse } from '@/types'
import { tasksApi } from '@/api/tasks'
import { useStoreAction, useStoreList } from '@/composables/useStoreAction'

export const useTaskStore = defineStore(
  'tasks',
  () => {
    const listHelper = useStoreList<Task>({ defaultPageSize: 20 })
    const actionHelper = useStoreAction()
    const currentTask = ref<Task | null>(null)

    const hasMore = computed(() => listHelper.items.value.length < listHelper.total.value)
    const activeTaskCount = computed(
      () => listHelper.items.value.filter((t) => t.is_active).length
    )
    const loading = computed(() => listHelper.loading.value || actionHelper.loading.value)
    const error = computed(() => listHelper.error.value || actionHelper.error.value)

    async function fetchTasks(params?: PaginationParams & { enabled?: boolean; script_id?: number }) {
      if (params?.page !== undefined) {
        listHelper.setPage(params.page)
      }
      await listHelper.load(
        async ({ page, pageSize }) => {
          const response: PaginatedResponse<Task> = await tasksApi.list({
            page,
            page_size: pageSize,
            ...params,
          })
          return { items: response.items, total: response.total }
        },
        { errorMessage: '获取任务列表失败' }
      )
    }

    async function fetchTask(taskId: number) {
      const result = await actionHelper.execute(
        async () => {
          currentTask.value = await tasksApi.getDetail(taskId)
          return currentTask.value
        },
        { errorMessage: '获取任务详情失败' }
      )
      return result
    }

    async function createTask(data: Partial<Task>) {
      const result = await actionHelper.execute(
        async () => {
          const newTask = await tasksApi.create(data)
          listHelper.items.value.unshift(newTask)
          listHelper.total.value++
          return newTask
        },
        { errorMessage: '创建任务失败' }
      )
      return result
    }

    async function updateTask(taskId: number, data: Partial<Task>) {
      const result = await actionHelper.execute(
        async () => {
          const updated = await tasksApi.update(taskId, data)
          const index = listHelper.items.value.findIndex((t) => t.id === taskId)
          if (index !== -1) {
            listHelper.items.value[index] = updated
          }
          if (currentTask.value?.id === taskId) {
            currentTask.value = updated
          }
          return updated
        },
        { errorMessage: '更新任务失败' }
      )
      return result
    }

    async function deleteTask(taskId: number) {
      await actionHelper.execute(
        async () => {
          await tasksApi.delete(taskId)
          listHelper.items.value = listHelper.items.value.filter((t) => t.id !== taskId)
          listHelper.total.value--
          if (currentTask.value?.id === taskId) {
            currentTask.value = null
          }
        },
        { errorMessage: '删除任务失败' }
      )
    }

    async function toggleTask(taskId: number) {
      const result = await actionHelper.execute(
        async () => {
          const updated = await tasksApi.toggle(taskId)
          const index = listHelper.items.value.findIndex((t) => t.id === taskId)
          if (index !== -1) {
            listHelper.items.value[index] = updated
          }
          if (currentTask.value?.id === taskId) {
            currentTask.value = updated
          }
          return updated
        },
        { errorMessage: '切换任务状态失败' }
      )
      return result
    }

    function reset() {
      listHelper.reset()
      actionHelper.reset()
      currentTask.value = null
    }

    return {
      tasks: listHelper.items,
      currentTask,
      total: listHelper.total,
      loading,
      error,
      page: listHelper.page,
      pageSize: listHelper.pageSize,
      hasMore,
      activeTaskCount,
      fetchTasks,
      fetchTask,
      createTask,
      updateTask,
      deleteTask,
      toggleTask,
      setPage: listHelper.setPage,
      setPageSize: listHelper.setPageSize,
      reset,
    }
  },
  {
    persist: {
      paths: ['page', 'pageSize'],
    },
  }
)
