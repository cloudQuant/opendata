import request from '@/utils/request'
import type {
  Execution,
  PaginationParams,
  PaginatedResponse,
  ExecutionStats,
} from '@/types'

export const dataApi = {
  // Trigger data download
  async download(interfaceId: number, parameters: Record<string, unknown>): Promise<DataDownloadResult> {
    const result = await request<DataDownloadResult, DataDownloadResult>({
      url: '/data/download',
      method: 'POST',
      data: { interface_id: interfaceId, parameters },
      // A download is accepted only when the server confirms task creation.
      validateStatus: (status) => status === 202,
    })

    if (!result || !Number.isSafeInteger(result.execution_id) || result.execution_id <= 0) {
      throw new Error('服务器未返回有效的下载任务信息')
    }

    return result
  },

  // Get execution detail
  getExecution(executionId: number): Promise<Execution> {
    return request({
      url: `/executions/${executionId}`,
      method: 'GET',
    })
  },

  // Get execution list
  listExecutions(
    params?: PaginationParams & { task_id?: number; script_id?: string; status?: string }
  ): Promise<PaginatedResponse<Execution>> {
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
}

export interface DataDownloadResult {
  execution_id: number
  status: string
  message?: string | null
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
  // The interceptor has already unwrapped the envelope, so this is `{count, failures}`.
  async failures(params: { domain?: string; source?: string; limit?: number } = {}): Promise<FailuresResponse> {
    const payload = await request.get<unknown, FailuresResponse | undefined>(
      '/pipeline/failures',
      { params },
    )
    return payload ?? { count: 0, failures: [] }
  },

  // Reset failed shards so the next run of their window retries them.
  async retryFailed(): Promise<{ reset: number }> {
    const payload = await request.post<unknown, { reset?: number } | undefined>(
      '/pipeline/retry-failed',
    )
    return { reset: payload?.reset ?? 0 }
  },
}
