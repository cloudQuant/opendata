/**
 * The reviewed run-control client.
 *
 * This module is deliberately not in `src/api/`: `src/api/*.ts` is the browser's
 * contract with `opendata.main:app` (that is what
 * `tests/test_frontend_endpoint_contract.py` compares against the mounted route
 * table), and `/reviewed/*` is not answered by that process. It is answered by
 * `create_reviewed_app`, which composes the reviewed router under the same
 * `/api/v1` prefix — so these paths keep using the shared client, its auth
 * header and its envelope unwrapping, and only the deployment has to route them
 * to the reviewed process. Nothing here runs until that runtime exists: every
 * method rejects with `reviewed_runtime_not_enabled` while
 * `isReviewedRuntimeEnabled()` is false, and the store never reaches them.
 */
import request from '@/utils/request'
import { isReviewedRuntimeEnabled, REVIEWED_RUNTIME_NOT_ENABLED } from '@/reviewed/runtime'

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
    if (!isReviewedRuntimeEnabled()) return Promise.reject(new Error(REVIEWED_RUNTIME_NOT_ENABLED))
    return request.get<unknown, { jobs: ReviewedJob[] }>('/reviewed/jobs', { signal })
  },

  createRun(payload: CreateReviewedRunRequest, signal?: AbortSignal): Promise<ReviewedRunAccepted> {
    if (!isReviewedRuntimeEnabled()) return Promise.reject(new Error(REVIEWED_RUNTIME_NOT_ENABLED))
    return request.post<unknown, ReviewedRunAccepted>('/reviewed/runs', payload, { signal })
  },

  async getRun(runKey: string, signal?: AbortSignal): Promise<ReviewedRunView> {
    if (!isReviewedRuntimeEnabled()) throw new Error(REVIEWED_RUNTIME_NOT_ENABLED)
    return request.get<unknown, ReviewedRunView>(`/reviewed/runs/${requireRunKey(runKey)}`, {
      signal,
    })
  },

  async cancelRun(runKey: string, signal?: AbortSignal): Promise<CancelReviewedRunResponse> {
    if (!isReviewedRuntimeEnabled()) throw new Error(REVIEWED_RUNTIME_NOT_ENABLED)
    return request.post<unknown, CancelReviewedRunResponse>(
      `/reviewed/runs/${requireRunKey(runKey)}/cancel`,
      undefined,
      { signal }
    )
  },
}
