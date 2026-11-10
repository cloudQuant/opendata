// Client for the ported AFI data-management plane mounted under /api/v1/afi-data.
// The ported routers keep AFI's bare `{items,total,page,page_size}` envelopes and
// path suffixes, so this client is a thin, typed wrapper over the shared axios
// instance rather than a re-typed duplicate of the native OpenData clients.
import request from '@/utils/request'
import type {
  DataScript,
  DataTable,
  Execution,
  ExecutionStats,
  PaginatedResponse,
  PaginationParams,
  Task,
} from '@/types'

const BASE = '/afi-data'

export interface ScheduleTemplate {
  value: string
  label: string
  description: string
  cron_expression: string
}

export interface ExecutionActionResult {
  execution_id: string
  status: string
}

export interface GovernanceProvider {
  id: string
  provider_id: string
  name: string
  category: string
  auth_type: string
  is_active: boolean
}

export interface GovernanceEndpoint {
  id: string
  endpoint_id: string
  endpoint_name: string
  provider_id: string
  is_active: boolean
}

export interface GovernanceJob {
  id: string
  job_id: string
  endpoint_id: string
  status: string
  created_at: string
}

export interface TableSchemaColumn {
  name: string
  type: string
  nullable: boolean
  default: unknown
}

export interface TableSchemaResponse {
  table_name: string
  columns: TableSchemaColumn[]
  row_count: number
  last_update_time: string | null
  data_available: boolean
  error: string | null
}

export interface TableRowsResponse {
  table_name: string
  columns: string[]
  rows: Record<string, unknown>[]
  page: number
  page_size: number
  total: number | null
  total_source: string | null
  total_age_seconds: number | null
  ordering: Record<string, unknown> | null
  data_available: boolean
  error: string | null
}

export const afiDataApi = {
  // --- scripts -------------------------------------------------------------
  listScripts(
    params?: PaginationParams & { category?: string; keyword?: string; is_active?: boolean }
  ): Promise<PaginatedResponse<DataScript>> {
    return request({ url: `${BASE}/scripts`, method: 'GET', params })
  },
  listScriptCategories(): Promise<string[]> {
    return request({ url: `${BASE}/scripts/categories`, method: 'GET' })
  },
  getScriptStats(): Promise<Record<string, number>> {
    return request({ url: `${BASE}/scripts/stats`, method: 'GET' })
  },
  scanScripts(): Promise<Record<string, unknown>> {
    return request({ url: `${BASE}/scripts/scan`, method: 'POST' })
  },
  getScript(scriptId: string): Promise<DataScript> {
    return request({ url: `${BASE}/scripts/${encodeURIComponent(scriptId)}`, method: 'GET' })
  },
  toggleScript(scriptId: string): Promise<DataScript> {
    return request({
      url: `${BASE}/scripts/${encodeURIComponent(scriptId)}/toggle`,
      method: 'PUT',
    })
  },
  runScript(scriptId: string, parameters: Record<string, unknown>): Promise<ExecutionActionResult> {
    return request({
      url: `${BASE}/scripts/${encodeURIComponent(scriptId)}/run`,
      method: 'POST',
      data: { parameters },
    })
  },
  createScript(payload: Partial<DataScript>): Promise<DataScript> {
    return request({ url: `${BASE}/scripts/admin/scripts`, method: 'POST', data: payload })
  },
  updateScript(scriptId: string, payload: Partial<DataScript>): Promise<DataScript> {
    return request({
      url: `${BASE}/scripts/admin/scripts/${encodeURIComponent(scriptId)}`,
      method: 'PUT',
      data: payload,
    })
  },
  deleteScript(scriptId: string): Promise<void> {
    return request({
      url: `${BASE}/scripts/admin/scripts/${encodeURIComponent(scriptId)}`,
      method: 'DELETE',
    })
  },

  // --- scheduled tasks -----------------------------------------------------
  getScheduleTemplates(): Promise<{ templates: ScheduleTemplate[] }> {
    return request({ url: `${BASE}/tasks/schedule/templates`, method: 'GET' })
  },
  listTasks(params?: PaginationParams & { is_active?: boolean }): Promise<PaginatedResponse<Task>> {
    return request({ url: `${BASE}/tasks`, method: 'GET', params })
  },
  createTask(payload: Partial<Task>): Promise<Task> {
    return request({ url: `${BASE}/tasks`, method: 'POST', data: payload })
  },
  getTask(taskId: number): Promise<Task> {
    return request({ url: `${BASE}/tasks/${taskId}`, method: 'GET' })
  },
  updateTask(taskId: number, payload: Partial<Task>): Promise<Task> {
    return request({ url: `${BASE}/tasks/${taskId}`, method: 'PUT', data: payload })
  },
  deleteTask(taskId: number): Promise<void> {
    return request({ url: `${BASE}/tasks/${taskId}`, method: 'DELETE' })
  },
  toggleTask(taskId: number): Promise<Task> {
    return request({ url: `${BASE}/tasks/${taskId}/toggle`, method: 'PATCH' })
  },
  runTask(taskId: number): Promise<ExecutionActionResult> {
    return request({ url: `${BASE}/tasks/${taskId}/run`, method: 'POST' })
  },
  listTaskExecutions(
    taskId: number,
    params?: PaginationParams
  ): Promise<PaginatedResponse<Execution>> {
    return request({ url: `${BASE}/tasks/${taskId}/executions`, method: 'GET', params })
  },

  // --- executions ----------------------------------------------------------
  listExecutions(
    params?: PaginationParams & {
      task_id?: number
      script_id?: string
      status?: string
      start_date?: string
      end_date?: string
    }
  ): Promise<PaginatedResponse<Execution>> {
    return request({ url: `${BASE}/executions`, method: 'GET', params })
  },
  getExecutionStats(): Promise<ExecutionStats> {
    return request({ url: `${BASE}/executions/stats`, method: 'GET' })
  },
  getRecentExecutions(limit = 20): Promise<Execution[]> {
    return request({ url: `${BASE}/executions/recent`, method: 'GET', params: { limit } })
  },
  getRunningExecutions(): Promise<Execution[]> {
    return request({ url: `${BASE}/executions/running`, method: 'GET' })
  },
  getExecution(executionId: string): Promise<Execution> {
    return request({ url: `${BASE}/executions/${encodeURIComponent(executionId)}`, method: 'GET' })
  },
  retryExecution(executionId: string): Promise<ExecutionActionResult> {
    return request({
      url: `${BASE}/executions/${encodeURIComponent(executionId)}/retry`,
      method: 'POST',
    })
  },

  // --- tables --------------------------------------------------------------
  listTables(
    params?: PaginationParams & { search?: string }
  ): Promise<PaginatedResponse<DataTable>> {
    return request({ url: `${BASE}/tables`, method: 'GET', params })
  },
  getTable(tableId: number): Promise<DataTable> {
    return request({ url: `${BASE}/tables/${tableId}`, method: 'GET' })
  },
  getTableSchema(tableId: number): Promise<TableSchemaResponse> {
    return request({ url: `${BASE}/tables/${tableId}/schema`, method: 'GET' })
  },
  getTableRows(
    tableId: number,
    params?: PaginationParams & { adjustment?: string }
  ): Promise<TableRowsResponse> {
    return request({ url: `${BASE}/tables/${tableId}/data`, method: 'GET', params })
  },

  // --- data governance -----------------------------------------------------
  bootstrapGovernance(): Promise<Record<string, unknown>> {
    return request({ url: `${BASE}/data-governance/bootstrap`, method: 'POST' })
  },
  listGovernanceProviders(): Promise<GovernanceProvider[]> {
    return request({ url: `${BASE}/data-governance/providers`, method: 'GET' })
  },
  listGovernanceEndpoints(providerId?: string): Promise<GovernanceEndpoint[]> {
    return request({
      url: `${BASE}/data-governance/endpoints`,
      method: 'GET',
      params: providerId ? { provider_id: providerId } : undefined,
    })
  },
  previewEndpoint(
    endpointId: string,
    params: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    return request({
      url: `${BASE}/data-governance/endpoints/${encodeURIComponent(endpointId)}/preview`,
      method: 'POST',
      data: { params },
    })
  },
  listGovernanceJobs(endpointId?: string): Promise<GovernanceJob[]> {
    return request({
      url: `${BASE}/data-governance/jobs`,
      method: 'GET',
      params: endpointId ? { endpoint_id: endpointId } : undefined,
    })
  },
  getGovernanceJob(jobId: string): Promise<GovernanceJob> {
    return request({
      url: `${BASE}/data-governance/jobs/${encodeURIComponent(jobId)}`,
      method: 'GET',
    })
  },
  createGovernanceJob(endpointId: string, params: Record<string, unknown>): Promise<GovernanceJob> {
    return request({
      url: `${BASE}/data-governance/endpoints/${encodeURIComponent(endpointId)}/jobs`,
      method: 'POST',
      data: { params },
    })
  },
}
