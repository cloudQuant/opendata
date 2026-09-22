import request from '@/utils/request'
import type {
  DataScript,
  ExecutionStats,
  PaginationParams,
  PaginatedResponse,
} from '@/types'

export const scriptApi = {
  // Get script list
  list(params?: PaginationParams & { category?: string; keyword?: string }): Promise<PaginatedResponse<DataScript>> {
    return request({
      url: '/scripts/',
      method: 'GET',
      params,
    })
  },

  // Get script detail
  getDetail(scriptId: string): Promise<DataScript> {
    return request({
      url: `/scripts/${scriptId}`,
      method: 'GET',
    })
  },

  // Get categories
  getCategories(): Promise<string[]> {
    return request({
      url: '/scripts/categories',
      method: 'GET',
    })
  },

  getStats(): Promise<ExecutionStats> {
    return request({
      url: '/scripts/stats',
      method: 'GET',
    })
  },

  // Toggle active status (admin)
  toggle(scriptId: string): Promise<DataScript> {
    return request({
      url: `/scripts/${scriptId}/toggle`,
      method: 'PUT',
    })
  },

  // Admin: Create custom script
  create(data: Partial<DataScript>): Promise<DataScript> {
    return request({
      url: '/scripts/admin/scripts',
      method: 'POST',
      data,
    })
  },

  // Admin: Update script
  update(scriptId: string, data: Partial<DataScript>): Promise<DataScript> {
    return request({
      url: `/scripts/admin/scripts/${scriptId}`,
      method: 'PUT',
      data,
    })
  },

  delete(scriptId: string): Promise<void> {
    return request({
      url: `/scripts/admin/scripts/${scriptId}`,
      method: 'DELETE',
    })
  },

  scan(): Promise<{ scanned?: number }> {
    return request({
      url: '/scripts/scan',
      method: 'POST',
    })
  },
}

// Legacy export
export const scriptsApi = scriptApi
