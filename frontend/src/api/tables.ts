import request from '@/utils/request'
import type {
  DataTable,
  TableDataResponse,
  TableSchema,
  PaginationParams,
  PaginatedResponse,
} from '@/types'

export const tablesApi = {
  // Get table list
  list(params?: PaginationParams & { search?: string }): Promise<PaginatedResponse<DataTable>> {
    return request({
      url: '/tables/',
      method: 'GET',
      params,
    })
  },

  // Get table schema
  getSchema(tableId: string | number): Promise<TableSchema> {
    return request({
      url: `/tables/${tableId}/schema`,
      method: 'GET',
    })
  },

  getData(
    tableId: string | number,
    page: number = 1,
    pageSize: number = 100
  ): Promise<TableDataResponse> {
    return request({
      url: `/tables/${tableId}/data`,
      method: 'GET',
      params: { page, page_size: pageSize },
    })
  },

  // Delete table (admin only)
  delete(tableId: string | number): Promise<void> {
    return request({
      url: `/tables/${tableId}`,
      method: 'DELETE',
    })
  },
}
