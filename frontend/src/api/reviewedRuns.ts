import request from '@/utils/request'

const RUN_KEY_PATTERN = /^[0-9a-f]{64}$/

export type ReviewedJobId = string | number
export type ReviewedJobVersion = string | number

export interface ReviewedJob {
  job_id: ReviewedJobId
  version: ReviewedJobVersion
  dataset_id: string
  provider: string
  model: string
  enabled: boolean
  reviewed: boolean
}

export type ReviewedRunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface ReviewedRunView {
  run_key: string
  status: ReviewedRunStatus
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  failure_code: null | 'run_failed'
}

export interface ReviewedRunAccepted {
  run_key: string
  status: ReviewedRunStatus
  job_id: ReviewedJobId
  version: ReviewedJobVersion
  created_at: string
}

export interface CreateReviewedRunRequest {
  job_id: ReviewedJobId
  version: ReviewedJobVersion
  idempotency_key: string
  query: Record<string, unknown>
  partition: Record<string, unknown>
  shard: null
}

export interface CancelReviewedRunResponse {
  run: ReviewedRunView
  outcome: 'cancelled' | 'terminal'
}

function requireRunKey(runKey: string): string {
  if (!RUN_KEY_PATTERN.test(runKey)) throw new Error('reviewed_run_key_invalid')
  return runKey
}

export const reviewedRunsApi = {
  listJobs(signal?: AbortSignal): Promise<{ jobs: ReviewedJob[] }> {
    return request.get<unknown, { jobs: ReviewedJob[] }>('/reviewed/jobs', { signal })
  },

  createRun(payload: CreateReviewedRunRequest, signal?: AbortSignal): Promise<ReviewedRunAccepted> {
    return request.post<unknown, ReviewedRunAccepted>('/reviewed/runs', payload, { signal })
  },

  async getRun(runKey: string, signal?: AbortSignal): Promise<ReviewedRunView> {
    return request.get<unknown, ReviewedRunView>(`/reviewed/runs/${requireRunKey(runKey)}`, {
      signal,
    })
  },

  async cancelRun(runKey: string, signal?: AbortSignal): Promise<CancelReviewedRunResponse> {
    return request.post<unknown, CancelReviewedRunResponse>(
      `/reviewed/runs/${requireRunKey(runKey)}/cancel`,
      undefined,
      { signal }
    )
  },
}
