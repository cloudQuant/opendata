import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const apiMocks = vi.hoisted(() => ({
  listJobs: vi.fn(),
  createRun: vi.fn(),
  getRun: vi.fn(),
  cancelRun: vi.fn(),
}))

vi.mock('@/api/reviewedRuns', () => ({ reviewedRunsApi: apiMocks }))

import { useReviewedRunsStore } from '@/stores/reviewedRuns'

const RUN_KEY = 'a'.repeat(64)
const CREATED_AT = '2026-10-27T00:00:00Z'

function makeJob(overrides: Record<string, unknown> = {}) {
  return {
    job_id: 'daily-equity',
    version: 'v3',
    dataset_id: 'equity.daily',
    provider: 'openbb',
    model: 'stock_daily',
    enabled: true,
    reviewed: true,
    ...overrides,
  }
}

function acceptedRun(overrides: Record<string, unknown> = {}) {
  return {
    run_key: RUN_KEY,
    status: 'queued',
    job_id: 'daily-equity',
    version: 'v3',
    created_at: CREATED_AT,
    ...overrides,
  }
}

function runView(status: string, overrides: Record<string, unknown> = {}) {
  return {
    run_key: RUN_KEY,
    status,
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
    started_at: null,
    finished_at: null,
    failure_code: null,
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  apiMocks.listJobs.mockResolvedValue({ jobs: [makeJob()] })
})

describe('reviewed runs store', () => {
  it('fails closed when a task is not reviewed or enabled', async () => {
    apiMocks.listJobs.mockResolvedValue({
      jobs: [makeJob({ reviewed: false }), makeJob({ job_id: 'disabled', enabled: false })],
    })
    const store = useReviewedRunsStore()
    await store.loadJobs()

    const rejected = await store.startRun({
      jobId: 'daily-equity',
      version: 'v3',
      query: {},
      partition: {},
    })

    expect(rejected).toBeNull()
    expect(store.runnableJobs).toEqual([])
    expect(store.runError).toMatch(/尚未审核/)
    expect(apiMocks.createRun).not.toHaveBeenCalled()
  })

  it('reuses one secure idempotency key after an ambiguous response', async () => {
    apiMocks.createRun
      .mockRejectedValueOnce(new Error('connection lost'))
      .mockRejectedValueOnce({ response: { data: { detail: 'idempotency_conflict' } } })
      .mockRejectedValueOnce({ response: { data: { detail: 'credential_secret' } } })
      .mockResolvedValueOnce(acceptedRun())
    const store = useReviewedRunsStore()
    await store.loadJobs()
    const input = {
      jobId: 'daily-equity',
      version: 'v3',
      query: { symbol: '600000' },
      partition: { trade_date: '2026-10-26' },
    }

    await expect(store.startRun(input)).resolves.toBeNull()
    const firstKey = store.pendingIntentKey
    expect(firstKey).toMatch(/^[0-9a-f]{64}$/)
    expect(store.runError).toContain('同一请求')

    await expect(store.startRun(input)).resolves.toBeNull()
    expect(store.runError).toBe('请求失败（idempotency_conflict）。')
    expect(store.pendingIntentKey).toBe(firstKey)

    await expect(store.startRun(input)).resolves.toBeNull()
    expect(store.runError).toBe('提交未确认；可用相同幂等键手动重试。')
    expect(store.runError).not.toContain('credential_secret')
    expect(store.pendingIntentKey).toBe(firstKey)

    await expect(store.startRun(input)).resolves.toMatchObject({ run_key: RUN_KEY })
    expect(apiMocks.createRun).toHaveBeenCalledTimes(4)
    expect(apiMocks.createRun.mock.calls[0][0]).toEqual({
      job_id: 'daily-equity',
      version: 'v3',
      idempotency_key: firstKey,
      query: input.query,
      partition: input.partition,
      shard: null,
    })
    expect(apiMocks.createRun.mock.calls[1][0].idempotency_key).toBe(firstKey)
    expect(apiMocks.createRun.mock.calls[2][0].idempotency_key).toBe(firstKey)
    expect(apiMocks.createRun.mock.calls[3][0].idempotency_key).toBe(firstKey)
    expect(store.pendingIntentKey).toBeNull()
    expect(store.activeRunKey).toBe(RUN_KEY)
  })

  it('rejects malformed run keys and fails closed on an unconfigured runtime', async () => {
    const store = useReviewedRunsStore()
    expect(store.selectRunKey('ABC123')).toBe(false)
    expect(apiMocks.getRun).not.toHaveBeenCalled()

    apiMocks.listJobs.mockRejectedValue({
      response: { data: { detail: 'reviewed_runtime_unavailable' } },
    })
    await expect(store.loadJobs()).resolves.toBe(false)
    expect(store.jobs).toEqual([])
    expect(store.jobsError).toContain('尚未配置')

    for (const detail of ['credential_secret', 'deadbeef']) {
      apiMocks.listJobs.mockRejectedValueOnce({ response: { data: { detail } } })
      await expect(store.loadJobs()).resolves.toBe(false)
      expect(store.jobsError).toBe('审核任务列表加载失败。')
      expect(store.jobsError).not.toContain(detail)
    }
  })

  it('ignores a late status response after the run context is reset', async () => {
    const gate = deferred<ReturnType<typeof runView>>()
    apiMocks.getRun.mockReturnValue(gate.promise)
    const store = useReviewedRunsStore()
    expect(store.selectRunKey(RUN_KEY)).toBe(true)
    const pending = store.fetchRun(RUN_KEY)
    store.reset()
    gate.resolve(runView('running'))

    await expect(pending).resolves.toBeNull()
    expect(store.currentRun).toBeNull()
    expect(store.activeRunKey).toBeNull()
  })

  it('accepts a ledger-fenced cancellation only when the returned run is terminal', async () => {
    apiMocks.createRun.mockResolvedValue(acceptedRun())
    apiMocks.getRun.mockResolvedValue(runView('running'))
    apiMocks.cancelRun.mockResolvedValue({
      run: runView('cancelled', { finished_at: '2026-10-27T00:02:00Z' }),
      outcome: 'cancelled',
    })
    const store = useReviewedRunsStore()
    await store.loadJobs()
    await store.startRun({ jobId: 'daily-equity', version: 'v3', query: {}, partition: {} })
    await store.fetchRun(RUN_KEY)

    await expect(store.cancelRun()).resolves.toMatchObject({
      outcome: 'cancelled',
      run: { status: 'cancelled' },
    })
    expect(store.currentRun?.status).toBe('cancelled')
  })
})
