import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { nextTick } from 'vue'
import ReviewedRunsView from '@/views/admin/ReviewedRunsView.vue'

const apiMocks = vi.hoisted(() => ({
  listJobs: vi.fn(),
  createRun: vi.fn(),
  getRun: vi.fn(),
  cancelRun: vi.fn(),
}))

vi.mock('@/api/reviewedRuns', () => ({ reviewedRunsApi: apiMocks }))

import { useAuthStore } from '@/stores/auth'

const RUN_KEY = 'a'.repeat(64)
const JOB = {
  job_id: 'daily-equity',
  version: 'v3',
  dataset_id: 'equity.daily',
  provider: 'openbb',
  model: 'stock_daily',
  enabled: true,
  reviewed: true,
}

function acceptedRun() {
  return {
    run_key: RUN_KEY,
    status: 'queued',
    job_id: JOB.job_id,
    version: JOB.version,
    created_at: '2026-10-27T00:00:00Z',
  }
}

function runView(status: string, overrides: Record<string, unknown> = {}) {
  return {
    run_key: RUN_KEY,
    status,
    created_at: '2026-10-27T00:00:00Z',
    updated_at: '2026-10-27T00:00:00Z',
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

async function settlePromises(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
}

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = {
    user_id: 1,
    email: 'admin@example.com',
    role: 'admin',
    created_at: '2026-10-27T00:00:00Z',
    updated_at: null,
  }
  auth.accessToken = 'test-token'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/login', component: { template: '<div />' } },
      { path: '/admin/reviewed-runs', component: ReviewedRunsView },
    ],
  })
  await router.push('/admin/reviewed-runs')
  await router.isReady()
  const wrapper = mount(ReviewedRunsView, { global: { plugins: [pinia, router] } })
  await settlePromises()
  return { wrapper, auth, router }
}

beforeEach(() => {
  vi.useRealTimers()
  vi.resetAllMocks()
  apiMocks.listJobs.mockResolvedValue({ jobs: [JOB] })
  apiMocks.createRun.mockResolvedValue(acceptedRun())
})

afterEach(() => {
  vi.useRealTimers()
})

describe('reviewed runs page polling', () => {
  it('keeps submission disabled when the directory contains only unreviewed or disabled jobs', async () => {
    apiMocks.listJobs.mockResolvedValue({
      jobs: [
        { ...JOB, reviewed: false },
        { ...JOB, job_id: 'disabled-job', enabled: false },
      ],
    })
    const { wrapper } = await mountPage()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('提交运行'))

    expect(wrapper.text()).toContain('目录中的任务均未审核或已停用')
    expect(submit?.attributes('disabled')).toBeDefined()
    expect(apiMocks.createRun).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('polls serially while active and stops after a terminal status', async () => {
    vi.useFakeTimers()
    const pendingFirstStatus = deferred<ReturnType<typeof runView>>()
    apiMocks.getRun
      .mockReturnValueOnce(pendingFirstStatus.promise)
      .mockResolvedValueOnce(runView('succeeded', { finished_at: '2026-10-27T00:03:00Z' }))
    const { wrapper } = await mountPage()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('提交运行'))
    await submit?.trigger('click')
    await settlePromises()
    expect(apiMocks.createRun).toHaveBeenCalledOnce()

    vi.advanceTimersByTime(2000)
    await settlePromises()
    expect(apiMocks.getRun).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(10_000)
    await settlePromises()
    expect(apiMocks.getRun).toHaveBeenCalledTimes(1)

    pendingFirstStatus.resolve(runView('running', { started_at: '2026-10-27T00:01:00Z' }))
    await settlePromises()
    vi.advanceTimersByTime(2000)
    await settlePromises()
    expect(apiMocks.getRun).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('成功')
    const cancelButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('请求取消'))
    expect(cancelButton?.attributes('disabled')).toBeDefined()

    vi.advanceTimersByTime(10_000)
    await settlePromises()
    expect(apiMocks.getRun).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('aborts in-flight polling and clears run state when the admin logs out', async () => {
    vi.useFakeTimers()
    const pendingStatus = deferred<ReturnType<typeof runView>>()
    apiMocks.getRun.mockReturnValue(pendingStatus.promise)
    const { wrapper, auth, router } = await mountPage()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('提交运行'))
    await submit?.trigger('click')
    await settlePromises()
    vi.advanceTimersByTime(2000)
    await settlePromises()
    expect(apiMocks.getRun).toHaveBeenCalledOnce()
    const requestSignal = apiMocks.getRun.mock.calls[0][1] as AbortSignal

    auth.logout()
    await settlePromises()

    expect(requestSignal.aborted).toBe(true)
    expect(router.currentRoute.value.path).toBe('/login')
    expect(wrapper.text()).not.toContain('排队中')
    wrapper.unmount()
  })
})
