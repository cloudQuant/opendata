import request from '@/utils/request'
import type {
  Execution,
  PaginationParams,
  PaginatedResponse,
  ExecutionStats,
} from '@/types'

export const dataApi = {
  // Trigger data download
  download(scriptId: number, parameters: Record<string, unknown>): Promise<{ execution_id: number; status: string }> {
    return request({
      url: '/data/download',
      method: 'POST',
      data: { script_id: scriptId, parameters },
    })
  },

  // Get execution detail
  getExecution(executionId: number): Promise<Execution> {
    return request({
      url: `/executions/${executionId}`,
      method: 'GET',
    })
  },

  // Get execution list
  listExecutions(params?: PaginationParams & { script_id?: number; status?: string }): Promise<PaginatedResponse<Execution>> {
    return request({
      url: '/executions/',
      method: 'GET',
      params,
    })
  },

  // Get execution stats
  getStats(params?: { start_date?: string; end_date?: string }): Promise<ExecutionStats> {
    return request({
      url: '/executions/stats',
      method: 'GET',
      params,
    })
  },

  // Get recent executions
  getRecent(limit: number = 50): Promise<Execution[]> {
    return request({
      url: '/executions/recent',
      method: 'GET',
      params: { limit },
    })
  },

  // Get running executions
  getRunning(): Promise<Execution[]> {
    return request({
      url: '/executions/running',
      method: 'GET',
    })
  },

  // Retry execution
  retry(executionId: number): Promise<{ execution_id: number }> {
    return request({
      url: `/executions/${executionId}/retry`,
      method: 'POST',
    })
  },
}

// ---------------------------------------------------------------------------
// Pipeline operations (B3.2 / AC-13: 失败清单一键重试)
// ---------------------------------------------------------------------------

export interface FailedShard {
  pipeline_id: string
  domain: string
  source: string
  shard: number
  window: { start: string; end: string }
  error: string | null
}

export interface FailuresResponse {
  count: number
  failures: FailedShard[]
}

export const pipelineApi = {
  // List the shards an earlier pipeline run left failed.
  async failures(params: { domain?: string; source?: string; limit?: number } = {}): Promise<FailuresResponse> {
    const response = await request.get<{ success: boolean; data: FailuresResponse }>(
      '/pipeline/failures',
      { params },
    )
    return response.data?.data ?? { count: 0, failures: [] }
  },

  // Reset failed shards so the next run of their window retries them.
  async retryFailed(): Promise<{ reset: number }> {
    const response = await request.post<{ success: boolean; data: { reset: number } }>(
      '/pipeline/retry-failed',
    )
    return response.data?.data ?? { reset: 0 }
  },
}
