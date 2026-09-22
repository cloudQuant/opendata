import request from '@/utils/request'
import type {
  Execution,
  Task,
  PaginationParams,
  PaginatedResponse,
} from '@/types'

export const tasksApi = {
  // Get task list
  list(params?: PaginationParams & { enabled?: boolean; script_id?: number }): Promise<PaginatedResponse<Task>> {
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

  // Toggle task enabled status
  toggle(taskId: number): Promise<Task> {
    return request({
      url: `/tasks/${taskId}/toggle`,
      method: 'PATCH',
    })
  },

  getExecutions(
    taskId: number,
    params?: PaginationParams
  ): Promise<PaginatedResponse<Execution>> {
    return request({
      url: `/tasks/${taskId}/executions`,
      method: 'GET',
      params,
    })
  },
}
