import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useTaskStore } from '@/stores/tasks'
import { tasksApi } from '@/api/tasks'
import type { Task, PaginatedResponse } from '@/types'

vi.mock('@/api/tasks', () => ({
  tasksApi: {
    list: vi.fn(),
    getDetail: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    toggle: vi.fn(),
  },
}))

describe('useTaskStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  const mockTask: Task = {
    id: 1,
    name: 'Test Task',
    description: 'Test Description',
    user_id: 1,
    script_id: 'test_script',
    script_name: 'Test Script',
    schedule_type: 'daily',
    schedule_expression: '09:00',
    parameters: {},
    is_active: true,
    retry_on_failure: true,
    max_retries: 3,
    timeout: 300,
    last_execution_at: null,
    next_execution_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  }

  describe('fetchTasks', () => {
    it('should fetch and store tasks', async () => {
    const mockResponse: PaginatedResponse<Task> = {
      items: [mockTask],
      total: 1,
      page: 1,
      page_size: 20,
    }

    vi.mocked(tasksApi.list).mockResolvedValue(mockResponse)

    const store = useTaskStore()
    await store.fetchTasks()

    expect(store.tasks).toHaveLength(1)
    expect(store.tasks[0]).toEqual(mockTask)
    expect(store.total).toBe(1)
  })

    it('should pass params to api', async () => {
    const mockResponse: PaginatedResponse<Task> = {
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    }

    vi.mocked(tasksApi.list).mockResolvedValue(mockResponse)

    const store = useTaskStore()
    await store.fetchTasks({ enabled: true, script_id: 123 })

    expect(tasksApi.list).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        script_id: 123,
      })
    )
  })

    it('should handle fetch errors', async () => {
    vi.mocked(tasksApi.list).mockRejectedValue(new Error('Network error'))

    const store = useTaskStore()

    await expect(store.fetchTasks()).rejects.toThrow()

    expect(store.error).toBeTruthy()
  })
})

  describe('fetchTask', () => {
    it('should fetch single task detail', async () => {
    vi.mocked(tasksApi.getDetail).mockResolvedValue(mockTask)

    const store = useTaskStore()
    const result = await store.fetchTask(1)

    expect(store.currentTask).toEqual(mockTask)
    expect(result).toEqual(mockTask)
    expect(tasksApi.getDetail).toHaveBeenCalledWith(1)
  })
})

  describe('createTask', () => {
    it('should create task and add to list', async () => {
    vi.mocked(tasksApi.create).mockResolvedValue(mockTask)

    const store = useTaskStore()
    store.total = 0

    const result = await store.createTask({ name: 'New Task' })

    expect(result).toEqual(mockTask)
    expect(store.tasks[0]).toEqual(mockTask)
    expect(store.total).toBe(1)
    expect(tasksApi.create).toHaveBeenCalledWith({ name: 'New Task' })
  })
})

  describe('updateTask', () => {
    it('should update task in list', async () => {
    const updatedTask = { ...mockTask, name: 'Updated Name' }
    vi.mocked(tasksApi.update).mockResolvedValue(updatedTask)

    const store = useTaskStore()
    store.tasks = [mockTask]

    const result = await store.updateTask(1, { name: 'Updated Name' })

    expect(result).toEqual(updatedTask)
    expect(store.tasks[0].name).toBe('Updated Name')
  })

    it('should update currentTask if same id', async () => {
    const updatedTask = { ...mockTask, name: 'Updated Name' }
    vi.mocked(tasksApi.update).mockResolvedValue(updatedTask)

    const store = useTaskStore()
    store.tasks = [mockTask]
    store.currentTask = mockTask

    await store.updateTask(1, { name: 'Updated Name' })

    expect(store.currentTask?.name).toBe('Updated Name')
  })
})

  describe('deleteTask', () => {
    it('should delete task from list', async () => {
    vi.mocked(tasksApi.delete).mockResolvedValue(undefined)

    const store = useTaskStore()
    store.tasks = [mockTask]
    store.total = 1

    await store.deleteTask(1)

    expect(store.tasks).toHaveLength(0)
    expect(store.total).toBe(0)
    expect(tasksApi.delete).toHaveBeenCalledWith(1)
  })

    it('should clear currentTask if deleted', async () => {
    vi.mocked(tasksApi.delete).mockResolvedValue(undefined)

    const store = useTaskStore()
    store.tasks = [mockTask]
    store.currentTask = mockTask

    await store.deleteTask(1)

    expect(store.currentTask).toBeNull()
  })
})

  describe('toggleTask', () => {
    it('should toggle task active status', async () => {
    const toggledTask = { ...mockTask, is_active: false }
    vi.mocked(tasksApi.toggle).mockResolvedValue(toggledTask)

    const store = useTaskStore()
    store.tasks = [mockTask]

    const result = await store.toggleTask(1)

    expect(result).toEqual(toggledTask)
    expect(store.tasks[0].is_active).toBe(false)
    expect(tasksApi.toggle).toHaveBeenCalledWith(1)
  })
})

  describe('reset', () => {
    it('should reset all state', async () => {
      const store = useTaskStore()
      store.tasks = [mockTask]
      store.currentTask = mockTask
      store.total = 1

      store.reset()

      expect(store.tasks).toHaveLength(0)
      expect(store.currentTask).toBeNull()
      expect(store.total).toBe(0)
    })
  })

  describe('computed properties', () => {
    it('should compute hasMore correctly', () => {
      const store = useTaskStore()
      store.tasks = [mockTask]
      store.total = 10

      expect(store.hasMore).toBe(true)

      store.total = 1

      expect(store.hasMore).toBe(false)
    })

    it('should compute activeTaskCount correctly', () => {
      const store = useTaskStore()
      store.tasks = [
        { ...mockTask, id: 1, is_active: true },
        { ...mockTask, id: 2, is_active: false },
        { ...mockTask, id: 3, is_active: true },
      ]

      expect(store.activeTaskCount).toBe(2)
    })
  })
})
