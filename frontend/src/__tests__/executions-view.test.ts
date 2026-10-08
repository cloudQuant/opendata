import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick, reactive } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { dataApi, pipelineApi, type FailedShard } from '@/api/data'
import ExecutionsView from '@/views/ExecutionsView.vue'
import { useExecutionStore } from '@/stores/executions'
import type { Execution, ExecutionStats, PaginatedResponse } from '@/types'
import request from '@/utils/request'

const mocks = vi.hoisted(() => ({
  route: {} as { query: Record<string, unknown> },
  success: vi.fn(),
  error: vi.fn(),
  apiError: vi.fn(),
  createObjectURL: vi.fn(),
  revokeObjectURL: vi.fn(),
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return {
    ...actual,
    useRoute: () => mocks.route,
  }
})

vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>('element-plus')
  return {
    ...actual,
    ElMessage: { success: mocks.success, error: mocks.error },
  }
})

vi.mock('@/utils/logger', () => ({
  logger: { apiError: mocks.apiError },
}))

const requests: Array<{ url: string; method: string; params: unknown }> = []
let nextEnvelope: { success: boolean; message?: string; data?: unknown } = {
  success: true,
  data: null,
}
const originalAdapter = request.defaults.adapter
const adapter: AxiosAdapter = async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
  requests.push({
    url: request.getUri(config),
    method: (config.method ?? 'get').toLowerCase(),
    params: config.params,
  })
  return {
    data: nextEnvelope,
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

function answer(data: unknown) {
  nextEnvelope = { success: true, message: 'success', data }
}

beforeAll(() => {
  request.defaults.adapter = adapter
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
})

const EXECUTION: Execution = {
  id: 7101,
  execution_id: 'c32-exec-ok',
  task_id: 71,
  script_id: 'c32_alpha',
  status: 'completed',
  start_time: '2026-09-25T02:00:00Z',
  end_time: '2026-09-25T02:00:12Z',
  duration: 12,
  error_message: null,
  rows_before: 11000,
  rows_after: 12345,
  retry_count: 0,
  triggered_by: 'scheduler',
  created_at: '2026-09-25T02:00:00Z',
}

const OTHER_EXECUTION: Execution = {
  ...EXECUTION,
  id: 7201,
  execution_id: 'c32-exec-other-task',
  task_id: 72,
  script_id: 'c32_beta',
  status: 'failed',
  error_message: 'C32ERR_OTHER_TASK',
  rows_before: null,
  rows_after: null,
}

const EXECUTION_PAGE: PaginatedResponse<Execution> = {
  items: [EXECUTION, OTHER_EXECUTION],
  total: 2,
  page: 1,
  page_size: 20,
}

const STATS: ExecutionStats = {
  total_count: 7,
  success_count: 6,
  failed_count: 1,
  success_rate: 85.7,
  avg_duration: 9.4,
  today_executions: 3,
}

const FAILURE: FailedShard = {
  pipeline_id: 'C32PIPE_LINE_1',
  domain: 'stock_daily',
  source: 'ths',
  shard: 3,
  window: { start: '2026-09-20', end: '2026-09-24' },
  error: 'C32FAIL_SHARD_TIMEOUT',
}

const routeState = reactive({ query: {} as Record<string, unknown> })

function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ').trim()
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
}

function executionsPage(items: Execution[] = EXECUTION_PAGE.items): PaginatedResponse<Execution> {
  return { items, total: items.length, page: 1, page_size: 20 }
}

function setupApiMocks() {
  vi.spyOn(dataApi, 'listExecutions').mockResolvedValue(EXECUTION_PAGE)
  vi.spyOn(dataApi, 'getStats').mockResolvedValue(STATS)
  vi.spyOn(pipelineApi, 'failures').mockResolvedValue({ count: 1, failures: [FAILURE] })
  vi.spyOn(pipelineApi, 'retryFailed').mockResolvedValue({ reset: 1 })
}

async function mountExecutions() {
  setupApiMocks()
  const wrapper = mount(ExecutionsView)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  requests.length = 0
  nextEnvelope = { success: true, data: null }
  routeState.query = {}
  mocks.route = routeState
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

enableAutoUnmount(afterEach)

describe('dataApi.listExecutions query serialization', () => {
  it('sends the backend string script identity over the real axios interceptor', async () => {
    answer(EXECUTION_PAGE)

    const result = await dataApi.listExecutions({
      page: 2,
      page_size: 10,
      script_id: 'c32_alpha',
    })

    expect(result).toEqual(EXECUTION_PAGE)
    expect(requests).toEqual([
      {
        url: '/api/v1/executions/?page=2&page_size=10&script_id=c32_alpha',
        method: 'get',
        params: { page: 2, page_size: 10, script_id: 'c32_alpha' },
      },
    ])
  })

  it('keeps the backend script_id string intact through the execution store request', async () => {
    answer(EXECUTION_PAGE)

    const store = useExecutionStore()
    store.setFilters({ script_id: 'c32_alpha' })
    await store.fetchExecutions()

    expect(store.executions).toEqual([EXECUTION, OTHER_EXECUTION])
    expect(requests).toEqual([
      {
        url: '/api/v1/executions/?page=1&page_size=20&script_id=c32_alpha',
        method: 'get',
        params: { page: 1, page_size: 20, script_id: 'c32_alpha' },
      },
    ])
  })
})

describe('ExecutionsView', () => {
  it('loads execution and failure data with their backend query contracts', async () => {
    const wrapper = await mountExecutions()

    expect(dataApi.listExecutions).toHaveBeenCalledTimes(1)
    expect(dataApi.listExecutions).toHaveBeenCalledWith({ page: 1, page_size: 20 })
    expect(dataApi.getStats).toHaveBeenCalledTimes(1)
    expect(pipelineApi.failures).toHaveBeenCalledWith({ limit: 100 })
    expect(flat(wrapper)).toContain('C32PIPE_LINE_1')
    expect(flat(wrapper)).toContain('C32FAIL_SHARD_TIMEOUT')
    expect(flat(wrapper)).toContain('C32ERR_OTHER_TASK')
    expect(flat(wrapper)).toContain('11000 → 12345')
    expect(flat(wrapper)).toContain('85.7%')
  })

  it('shows clear empty states when both histories have no rows', async () => {
    setupApiMocks()
    vi.mocked(dataApi.listExecutions).mockResolvedValue(executionsPage([]))
    vi.mocked(pipelineApi.failures).mockResolvedValue({ count: 0, failures: [] })
    const wrapper = mount(ExecutionsView)
    await flushPromises()

    expect(flat(wrapper)).toContain('暂无失败分片')
    expect(wrapper.find('.el-table__empty-text').exists()).toBe(true)
  })

  it('renders execution-list errors and retries with the same task filter', async () => {
    routeState.query = { task_id: '71' }
    setupApiMocks()
    vi.mocked(dataApi.listExecutions).mockRejectedValueOnce(new Error('execution list unavailable'))
    const wrapper = mount(ExecutionsView)
    await flushPromises()

    expect(flat(wrapper)).toContain('execution list unavailable')
    expect(dataApi.listExecutions).toHaveBeenNthCalledWith(1, {
      page: 1,
      page_size: 20,
      task_id: 71,
    })
    await button(wrapper, '重试')!.trigger('click')
    await flushPromises()

    expect(dataApi.listExecutions).toHaveBeenNthCalledWith(2, {
      page: 1,
      page_size: 20,
      task_id: 71,
    })
    expect(flat(wrapper)).toContain('c32_alpha')
  })

  it('shows task-scoped history while keeping global statistics and failure data explicit', async () => {
    routeState.query = { task_id: '71' }
    const wrapper = await mountExecutions()

    expect(dataApi.listExecutions).toHaveBeenCalledExactlyOnceWith({
      page: 1,
      page_size: 20,
      task_id: 71,
    })
    expect(flat(wrapper)).toContain('当前任务：71')
    expect(flat(wrapper)).toContain('统计卡片和失败分片仍为全局数据')
    expect(flat(wrapper)).toContain('C32FAIL_SHARD_TIMEOUT')
  })

  const invalidTaskFilters: Array<[string | string[], string]> = [
    ['unknown', 'unknown task id'],
    ['071', 'non-canonical task id'],
    ['0', 'non-positive task id'],
    ['9007199254740992', 'unsafe task id'],
    [['71', '72'], 'duplicate task id'],
  ]

  it.each(invalidTaskFilters)('rejects %s as a task filter without querying all executions (%s)', async (value) => {
    routeState.query = { task_id: value }
    const wrapper = await mountExecutions()

    expect(dataApi.listExecutions).not.toHaveBeenCalled()
    expect(flat(wrapper)).toContain('任务 ID 无效')
    expect(flat(wrapper)).not.toContain('c32_alpha')
  })

  it('resets pagination and reloads when the route task id changes, without stale results winning', async () => {
    routeState.query = { task_id: '71' }
    setupApiMocks()
    const firstRequest = deferred<PaginatedResponse<Execution>>()
    const secondRequest = deferred<PaginatedResponse<Execution>>()
    vi.mocked(dataApi.listExecutions).mockImplementation((params) =>
      params?.task_id === 71 ? firstRequest.promise : secondRequest.promise
    )
    const wrapper = mount(ExecutionsView)
    await flushPromises()

    expect(dataApi.listExecutions).toHaveBeenCalledTimes(1)
    await wrapper.findComponent({ name: 'ElPagination' }).vm.$emit('current-change', 3)
    await flushPromises()
    expect(dataApi.listExecutions).toHaveBeenLastCalledWith({
      page: 3,
      page_size: 20,
      task_id: 71,
    })

    routeState.query = { task_id: '72' }
    await nextTick()
    await flushPromises()
    expect(dataApi.listExecutions).toHaveBeenLastCalledWith({
      page: 1,
      page_size: 20,
      task_id: 72,
    })
    secondRequest.resolve(executionsPage([OTHER_EXECUTION]))
    await flushPromises()
    firstRequest.resolve(executionsPage([EXECUTION]))
    await flushPromises()

    expect(flat(wrapper)).toContain('c32_beta')
    expect(flat(wrapper)).not.toContain('c32_alpha')
  })

  it('resets to page one on size change and retains the task id in the request', async () => {
    routeState.query = { task_id: '71' }
    const wrapper = await mountExecutions()

    await wrapper.findComponent({ name: 'ElPagination' }).vm.$emit('current-change', 3)
    await flushPromises()
    await wrapper.findComponent({ name: 'ElPagination' }).vm.$emit('size-change', 50)
    await flushPromises()

    expect(dataApi.listExecutions).toHaveBeenLastCalledWith({
      page: 1,
      page_size: 50,
      task_id: 71,
    })
  })

  it('reloads failures after a successful retry and reports a failed retry without false success', async () => {
    setupApiMocks()
    vi.mocked(pipelineApi.failures)
      .mockResolvedValueOnce({ count: 1, failures: [FAILURE] })
      .mockResolvedValueOnce({ count: 0, failures: [] })
    const wrapper = mount(ExecutionsView)
    await flushPromises()
    await button(wrapper, '一键重试')!.trigger('click')
    await flushPromises()

    expect(pipelineApi.retryFailed).toHaveBeenCalledTimes(1)
    expect(pipelineApi.failures).toHaveBeenNthCalledWith(1, { limit: 100 })
    expect(pipelineApi.failures).toHaveBeenNthCalledWith(2, { limit: 100 })
    expect(mocks.success).toHaveBeenCalledWith('已重置 1 个失败分片，下次运行将自动重试')
    expect(flat(wrapper)).toContain('暂无失败分片')

    vi.restoreAllMocks()
    vi.clearAllMocks()
    setupApiMocks()
    vi.mocked(pipelineApi.retryFailed).mockRejectedValueOnce(new Error('retry rejected'))
    const failedWrapper = mount(ExecutionsView)
    await flushPromises()
    await button(failedWrapper, '一键重试')!.trigger('click')
    await flushPromises()

    expect(mocks.error).toHaveBeenCalledWith('retry rejected')
    expect(mocks.success).not.toHaveBeenCalled()
    expect(pipelineApi.failures).toHaveBeenCalledTimes(1)
    expect(flat(failedWrapper)).toContain('C32FAIL_SHARD_TIMEOUT')
  })

  it('disables failure export and retry when loading failed shards fails', async () => {
    setupApiMocks()
    vi.mocked(pipelineApi.failures).mockRejectedValueOnce(new Error('failure list unavailable'))
    const wrapper = mount(ExecutionsView)
    await flushPromises()

    expect(flat(wrapper)).toContain('失败清单加载失败：failure list unavailable')
    expect(button(wrapper, '一键重试')).toBeUndefined()
    expect(button(wrapper, '导出 JSON')?.attributes('disabled')).toBeDefined()
  })

  it('exports the loaded failures as the backend-shaped JSON file', async () => {
    setupApiMocks()
    const wrapper = await mountExecutions()
    class URLWithObjectUrls extends URL {
      static override createObjectURL(blob: Blob | MediaSource) {
        return mocks.createObjectURL(blob)
      }
      static override revokeObjectURL(url: string) {
        mocks.revokeObjectURL(url)
      }
    }
    mocks.createObjectURL.mockReturnValue('blob:pipeline-failures')
    vi.stubGlobal('URL', URLWithObjectUrls)
    let downloaded: { href: string; name: string } | null = null
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloaded = { href: this.href, name: this.download }
    })

    await button(wrapper, '导出 JSON')!.trigger('click')

    expect(mocks.createObjectURL).toHaveBeenCalledTimes(1)
    expect(downloaded).toEqual({ href: 'blob:pipeline-failures', name: 'pipeline-failures.json' })
    const blob = mocks.createObjectURL.mock.calls[0][0] as Blob
    await expect(blob.text()).resolves.toBe(JSON.stringify({ count: 1, failures: [FAILURE] }, null, 2))
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}
