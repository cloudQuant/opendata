import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'

const toastMocks = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    accessToken: 'test-token',
    refreshToken: null,
    refreshAccessToken: vi.fn(),
    logout: vi.fn(),
  }),
}))

vi.mock('element-plus', () => ({ ElMessage: toastMocks }))

import request from '@/utils/request'
import { reviewedRunsApi } from '@/api/reviewedRuns'

const calls: Array<{
  url: string
  method: string
  data: unknown
  signal: InternalAxiosRequestConfig['signal']
}> = []
let responseBody: unknown
const originalAdapter = request.defaults.adapter

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig
): Promise<AxiosResponse> => {
  calls.push({
    url: `${config.baseURL ?? ''}${config.url ?? ''}`,
    method: (config.method ?? 'get').toLowerCase(),
    data: config.data,
    signal: config.signal,
  })
  return {
    data: { success: true, message: 'success', data: responseBody },
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

beforeEach(() => {
  calls.length = 0
  responseBody = null
  request.defaults.adapter = adapter
  vi.clearAllMocks()
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
})

describe('reviewed runs API wire contract', () => {
  it('reads the reviewed job directory through the shared API client', async () => {
    const signal = new AbortController().signal
    responseBody = { jobs: [] }

    await expect(reviewedRunsApi.listJobs(signal)).resolves.toEqual({ jobs: [] })
    expect(calls).toEqual([
      expect.objectContaining({
        url: '/api/v1/reviewed/jobs',
        method: 'get',
        signal,
      }),
    ])
  })

  it('sends the strict run DTO unchanged and forwards cancellation signals', async () => {
    const signal = new AbortController().signal
    const payload = {
      job_id: 'daily-equity',
      version: 'v3',
      idempotency_key: 'b'.repeat(64),
      query: { symbol: '600000' },
      partition: { trade_date: '2026-10-26' },
      shard: null,
    }
    responseBody = {
      run_key: 'a'.repeat(64),
      status: 'queued',
      job_id: 'daily-equity',
      version: 'v3',
      created_at: '2026-10-27T00:00:00Z',
    }

    await reviewedRunsApi.createRun(payload, signal)

    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({
      url: '/api/v1/reviewed/runs',
      method: 'post',
      signal,
    })
    expect(JSON.parse(String(calls[0].data))).toEqual(payload)
  })

  it('uses only the validated run key path and sends no cancel payload', async () => {
    const signal = new AbortController().signal
    const runKey = 'c'.repeat(64)
    responseBody = {
      run: {
        run_key: runKey,
        status: 'cancelled',
        created_at: '2026-10-27T00:00:00Z',
        updated_at: '2026-10-27T00:01:00Z',
        started_at: null,
        finished_at: '2026-10-27T00:01:00Z',
        failure_code: null,
      },
      outcome: 'cancelled',
    }

    await reviewedRunsApi.cancelRun(runKey, signal)

    expect(calls[0]).toMatchObject({
      url: `/api/v1/reviewed/runs/${runKey}/cancel`,
      method: 'post',
      signal,
    })
    expect(calls[0].data).toBeUndefined()
  })

  it('rejects malformed run keys before constructing a request path', async () => {
    await expect(reviewedRunsApi.getRun('../other')).rejects.toThrow('reviewed_run_key_invalid')
    await expect(reviewedRunsApi.cancelRun('A'.repeat(64))).rejects.toThrow(
      'reviewed_run_key_invalid'
    )
    expect(calls).toEqual([])
  })
})
