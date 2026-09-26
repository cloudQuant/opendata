import request from '@/utils/request'
import type {
  Task,
  PaginationParams,
  PaginatedResponse,
} from '@/types'

export const tasksApi = {
  // `is_active` is the backend's query name (opendata/api/tasks.py `list_tasks`); FastAPI
  // drops unknown query params, so a filter spelled any other way never filters.
  list(params?: PaginationParams & { is_active?: boolean }): Promise<PaginatedResponse<Task>> {
    return request({
      url: '/tasks/',
      method: 'GET',
      params,
    })
  },

  // Get task detail
  getDetail(taskId: number): Promise<Task> {
    return request({
      url: `/tasks/${taskId}`,
      method: 'GET',
    })
  },

  // Create task
  create(data: Partial<Task>): Promise<Task> {
    return request({
      url: '/tasks/',
      method: 'POST',
      data,
    })
  },

  // Update task
  update(taskId: number, data: Partial<Task>): Promise<Task> {
    return request({
      url: `/tasks/${taskId}`,
      method: 'PUT',
      data,
    })
  },

  // Delete task
  delete(taskId: number): Promise<void> {
    return request({
      url: `/tasks/${taskId}`,
      method: 'DELETE',
    })
  },
}
