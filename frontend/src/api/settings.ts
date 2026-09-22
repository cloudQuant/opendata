import request from '@/utils/request'
import type {
  DatabaseConfigResponse,
  TestConnectionResponse,
} from '@/types'

export interface DatabaseConfig {
  host: string
  port: number
  database: string
  user: string
  password?: string
}

export const settingsApi = {
  getDatabaseConfig(): Promise<DatabaseConfigResponse> {
    return request({
      url: '/settings/database',
      method: 'GET',
    })
  },

  getWarehouseConfig(): Promise<DatabaseConfigResponse> {
    return request({
      url: '/settings/database/warehouse',
      method: 'GET',
    })
  },

  testConnection(config: DatabaseConfig): Promise<TestConnectionResponse> {
    return request({
      url: '/settings/database/test',
      method: 'POST',
      data: config,
    })
  },

  testWarehouseConnection(config: DatabaseConfig): Promise<TestConnectionResponse> {
    return request({
      url: '/settings/database/warehouse/test',
      method: 'POST',
      data: config,
    })
  },

  updateDatabaseConfig(config: DatabaseConfig): Promise<DatabaseConfigResponse> {
    return request({
      url: '/settings/database',
      method: 'PUT',
      data: config,
    })
  },
}
