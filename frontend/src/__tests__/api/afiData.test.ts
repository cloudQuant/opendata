import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'

const toastMocks = vi.hoisted(() => ({ error: vi.fn() }))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ accessToken: 'test-token', refreshToken: null }),
}))
vi.mock('element-plus', () => ({ ElMessage: toastMocks }))

import request from '@/utils/request'
import { afiDataApi } from '@/api/afiData'

const calls: Array<{ url: string; method: string; params: unknown; data: unknown }> = []
const originalAdapter = request.defaults.adapter

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig
): Promise<AxiosResponse> => {
  calls.push({
    url: `${config.baseURL ?? ''}${config.url ?? ''}`,
    method: (config.method ?? 'get').toLowerCase(),
    params: config.params,
    data: config.data,
  })
  // The ported plane returns bare envelopes; the interceptor's `res.data ?? res`
  // passes them through unchanged.
  return { data: {}, status: 200, statusText: 'OK', headers: {}, config, request: {} }
}

beforeEach(() => {
  calls.length = 0
  request.defaults.adapter = adapter
})

describe('afiData client wire contract', () => {
  it('addresses every resource under the /afi-data namespace', async () => {
    await afiDataApi.listScripts()
    await afiDataApi.listTasks()
    await afiDataApi.listExecutions()
    await afiDataApi.listTables()
    await afiDataApi.listGovernanceProviders()
    const urls = calls.map((c) => c.url)
    expect(urls.every((u) => u.startsWith('/api/v1/afi-data/'))).toBe(true)
    expect(urls).toContain('/api/v1/afi-data/scripts')
    expect(urls).toContain('/api/v1/afi-data/tasks')
    expect(urls).toContain('/api/v1/afi-data/executions')
    expect(urls).toContain('/api/v1/afi-data/tables')
    expect(urls).toContain('/api/v1/afi-data/data-governance/providers')
  })

  it('keeps the AFI path suffixes and HTTP verbs', async () => {
    await afiDataApi.runScript('stock_zh_a_hist', { a: 1 })
    await afiDataApi.toggleTask(7)
    await afiDataApi.retryExecution('ak_exec_123')
    await afiDataApi.deleteScript('old_script')
    const byUrl = new Map(calls.map((c) => [c.url, c]))
    expect(byUrl.get('/api/v1/afi-data/scripts/stock_zh_a_hist/run')?.method).toBe('post')
    expect(byUrl.get('/api/v1/afi-data/tasks/7/toggle')?.method).toBe('patch')
    expect(byUrl.get('/api/v1/afi-data/executions/ak_exec_123/retry')?.method).toBe('post')
    expect(byUrl.get('/api/v1/afi-data/scripts/admin/scripts/old_script')?.method).toBe('delete')
  })

  it('wraps script run parameters in the ScriptRunRequest body', async () => {
    await afiDataApi.runScript('s1', { start_date: '20260101' })
    const call = calls.find((c) => c.url.endsWith('/scripts/s1/run'))
    // The request transformer has already JSON-serialised the body at the adapter.
    expect(JSON.parse(call?.data as string)).toEqual({
      parameters: { start_date: '20260101' },
    })
  })

  it('encodes path parameters and forwards query params', async () => {
    await afiDataApi.getTableRows(1049, { page: 2, page_size: 50, adjustment: 'qfq' })
    const call = calls.find((c) => c.url === '/api/v1/afi-data/tables/1049/data')
    expect(call?.params).toMatchObject({ page: 2, page_size: 50, adjustment: 'qfq' })
  })
})

request.defaults.adapter = originalAdapter
