import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { AxiosError, type AxiosAdapter, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import { dataApi } from '@/api/data'
import { interfacesApi, type DataInterfaceSummary } from '@/api/interfaces'
import { scriptsApi } from '@/api/scripts'
import ScriptDetailView from '@/views/ScriptDetailView.vue'
import type { DataScript } from '@/types'

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  requestGet: vi.fn(),
  routerPush: vi.fn(),
  routerBack: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}))

vi.mock('@/utils/request', () => ({
  default: Object.assign(mocks.request, { get: mocks.requestGet }),
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return {
    ...actual,
    useRoute: () => ({ params: { id: 'legacy-script' } }),
    useRouter: () => ({ push: mocks.routerPush, back: mocks.routerBack }),
  }
})

vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>('element-plus')
  return {
    ...actual,
    ElMessage: { success: mocks.success, error: mocks.error },
  }
})

const SCRIPT: DataScript = {
  id: 7,
  script_id: 'legacy-script',
  script_name: '旧版脚本文档',
  category: '股票数据',
  sub_category: null,
  frequency: 'daily',
  description: '保留原有脚本说明',
  source: 'ths',
  target_table: null,
  module_path: 'legacy.module',
  function_name: 'download',
  parameters: [],
  estimated_duration: 10,
  timeout: 60,
  is_active: true,
  is_custom: false,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function makeInterface(id: number, displayName: string): DataInterfaceSummary {
  return {
    id,
    name: `registered_${id}`,
    display_name: displayName,
    description: `说明：${displayName}`,
    category_name: '行情',
    is_active: true,
  }
}

const SAME_NUMBER_INTERFACE = makeInterface(7, '编号相同的其他数据能力')
const REAL_INTERFACE = makeInterface(91, '用户选择的真实数据接口')

function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ')
}

function createButton(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('button').find((button) => button.text().trim() === '创建下载任务')
}

async function mountDetail(available: DataInterfaceSummary[] = [SAME_NUMBER_INTERFACE, REAL_INTERFACE]) {
  vi.spyOn(scriptsApi, 'getDetail').mockResolvedValue(SCRIPT)
  vi.spyOn(interfacesApi, 'listEnabled').mockResolvedValue(available)
  if (!vi.isMockFunction(dataApi.download)) {
    vi.spyOn(dataApi, 'download').mockResolvedValue({ execution_id: 1, status: 'pending' })
  }
  const wrapper = mount(ScriptDetailView)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  mocks.request.mockReset()
  mocks.requestGet.mockReset()
  mocks.routerPush.mockReset().mockResolvedValue(undefined)
  mocks.routerBack.mockReset()
  mocks.success.mockReset()
  mocks.error.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('dataApi.download', () => {
  it('posts the selected interface id and parameters and accepts only HTTP 202', async () => {
    mocks.request.mockResolvedValueOnce({ execution_id: 123, status: 'pending' })

    const result = await dataApi.download(91, { symbol: '000001' })

    expect(result.execution_id).toBe(123)
    expect(mocks.request).toHaveBeenCalledTimes(1)
    const config = mocks.request.mock.calls[0][0] as AxiosRequestConfig
    expect(config).toMatchObject({
      url: '/data/download',
      method: 'POST',
      data: { interface_id: 91, parameters: { symbol: '000001' } },
    })
    expect(config.validateStatus?.(202)).toBe(true)
    for (const status of [200, 201, 400, 401, 404, 422, 500]) {
      expect(config.validateStatus?.(status)).toBe(false)
    }
    expect(config.data).not.toHaveProperty('script_id')
  })

  it('rejects a response without a valid execution id', async () => {
    mocks.request.mockResolvedValueOnce({ status: 'pending' })

    await expect(dataApi.download(91, {})).rejects.toThrow('服务器未返回有效的下载任务信息')
  })
})

describe('interfacesApi.listEnabled', () => {
  it('loads every active interface across pages', async () => {
    const firstPageItems = [
      SAME_NUMBER_INTERFACE,
      ...Array.from({ length: 99 }, (_, index) => makeInterface(1000 + index, `第一页接口 ${index}`)),
    ]
    mocks.requestGet
      .mockResolvedValueOnce({
        items: firstPageItems,
        total: 102,
        page: 1,
        page_size: 100,
        total_pages: 2,
      })
      .mockResolvedValueOnce({
        items: [REAL_INTERFACE, makeInterface(92, '第二页接口')],
        total: 102,
        page: 2,
        page_size: 100,
        total_pages: 2,
      })

    const result = await interfacesApi.listEnabled()

    expect(result).toHaveLength(102)
    expect(result.map((iface) => iface.id)).toContain(7)
    expect(result.map((iface) => iface.id)).toContain(91)
    expect(mocks.requestGet).toHaveBeenNthCalledWith(1, '/data/interfaces/', {
      params: { page: 1, page_size: 100, is_active: true },
    })
    expect(mocks.requestGet).toHaveBeenNthCalledWith(2, '/data/interfaces/', {
      params: { page: 2, page_size: 100, is_active: true },
    })
  })

  it('returns an empty catalog only after confirming the complete empty page', async () => {
    mocks.requestGet.mockResolvedValueOnce({
      items: [],
      total: 0,
      page: 1,
      page_size: 100,
      total_pages: 0,
    })

    await expect(interfacesApi.listEnabled()).resolves.toEqual([])
  })

  it('rejects incomplete pagination rather than reporting no interfaces', async () => {
    const firstPageItems = [
      SAME_NUMBER_INTERFACE,
      ...Array.from({ length: 99 }, (_, index) => makeInterface(2000 + index, `第一页接口 ${index}`)),
    ]
    mocks.requestGet
      .mockResolvedValueOnce({
        items: firstPageItems,
        total: 102,
        page: 1,
        page_size: 100,
        total_pages: 2,
      })
      .mockResolvedValueOnce({
        items: [],
        total: 102,
        page: 2,
        page_size: 100,
        total_pages: 2,
      })

    await expect(interfacesApi.listEnabled()).rejects.toThrow('分页结果不完整')
  })

  it('rejects catalogs beyond the bounded page limit after one request', async () => {
    mocks.requestGet.mockResolvedValueOnce({
      items: Array.from({ length: 100 }, (_, index) => makeInterface(3000 + index, `接口 ${index}`)),
      total: 10001,
      page: 1,
      page_size: 100,
      total_pages: 101,
    })

    await expect(interfacesApi.listEnabled()).rejects.toThrow('分页目录筛选后重试')
    expect(mocks.requestGet).toHaveBeenCalledTimes(1)
  })

  it('rejects malformed page and item fields instead of coercing them', async () => {
    const malformedPages: unknown[] = [
      null,
      { items: {}, total: 0, page: 1, page_size: 100, total_pages: 0 },
    ]

    for (const payload of malformedPages) {
      mocks.requestGet.mockResolvedValueOnce(payload)
      await expect(interfacesApi.listEnabled()).rejects.toThrow('无效分页信息')
    }

    const malformedItems: unknown[] = [
      { ...REAL_INTERFACE, id: 0 },
      { ...REAL_INTERFACE, is_active: 'false' },
      { ...REAL_INTERFACE, name: null },
      { ...REAL_INTERFACE, display_name: 91 },
    ]

    for (const item of malformedItems) {
      mocks.requestGet.mockResolvedValueOnce({
        items: [item],
        total: 1,
        page: 1,
        page_size: 100,
        total_pages: 1,
      })
      await expect(interfacesApi.listEnabled()).rejects.toThrow('无效或未启用的项目')
    }
  })
})

describe('ScriptDetailView download selection', () => {
  it('requires an explicit choice and never defaults from the script id', async () => {
    const wrapper = await mountDetail()
    const button = createButton(wrapper)

    expect(button).toBeDefined()
    expect(button?.attributes('disabled')).toBeDefined()
    await button?.trigger('click')

    expect(dataApi.download).not.toHaveBeenCalled()
    expect(mocks.success).not.toHaveBeenCalled()
    expect(mocks.routerPush).not.toHaveBeenCalled()
  })

  it('sends only the explicitly selected interface and navigates after acceptance', async () => {
    vi.spyOn(dataApi, 'download').mockResolvedValue({ execution_id: 456, status: 'pending' })
    const wrapper = await mountDetail()

    expect(flat(wrapper)).toContain('旧版脚本文档')
    expect(flat(wrapper)).not.toContain('91')
    const realOption = wrapper
      .findAllComponents({ name: 'ElOption' })
      .find((option) => option.props('value') === 91)
    expect(realOption?.props('label')).toBe('用户选择的真实数据接口')
    expect(flat(realOption!)).toContain('说明：用户选择的真实数据接口')

    const select = wrapper.findComponent({ name: 'ElSelect' })
    await select.vm.$emit('update:modelValue', 91)
    await flushPromises()
    await createButton(wrapper)?.trigger('click')
    await flushPromises()

    expect(dataApi.download).toHaveBeenCalledTimes(1)
    expect(dataApi.download).toHaveBeenCalledWith(91, {})
    expect(dataApi.download).not.toHaveBeenCalledWith(7, {})
    expect(mocks.success).toHaveBeenCalledWith('下载任务已创建')
    expect(mocks.routerPush).toHaveBeenCalledWith('/executions')
  })

  it('keeps task creation disabled when the catalog has no interfaces', async () => {
    const wrapper = await mountDetail([])

    expect(flat(wrapper)).toContain('当前没有可用的数据接口，无法创建下载任务')
    expect(createButton(wrapper)?.attributes('disabled')).toBeDefined()
    await createButton(wrapper)?.trigger('click')

    expect(dataApi.download).not.toHaveBeenCalled()
    expect(mocks.success).not.toHaveBeenCalled()
    expect(mocks.routerPush).not.toHaveBeenCalled()
  })

  it('shows catalog and download errors without claiming success or navigating', async () => {
    vi.spyOn(interfacesApi, 'listEnabled').mockRejectedValueOnce(new Error('catalog unavailable'))
    const wrapper = await mountDetail()

    expect(flat(wrapper)).toContain('catalog unavailable')
    expect(createButton(wrapper)?.attributes('disabled')).toBeDefined()

    vi.restoreAllMocks()
    vi.spyOn(scriptsApi, 'getDetail').mockResolvedValue(SCRIPT)
    vi.spyOn(interfacesApi, 'listEnabled').mockResolvedValue([REAL_INTERFACE])
    vi.spyOn(dataApi, 'download').mockRejectedValueOnce(new Error('Data interface is not active'))
    const retryWrapper = mount(ScriptDetailView)
    await flushPromises()
    await retryWrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 91)
    await createButton(retryWrapper)?.trigger('click')
    await flushPromises()

    expect(mocks.error).toHaveBeenCalledWith('Data interface is not active')
    expect(mocks.success).not.toHaveBeenCalled()
    expect(mocks.routerPush).not.toHaveBeenCalled()
  })
})

describe('dataApi.download through the real Axios client', () => {
  it('accepts the backend 202 body and rejects the same body with a non-202 status', async () => {
    vi.doUnmock('@/utils/request')
    vi.resetModules()

    const { createPinia, setActivePinia } = await import('pinia')
    setActivePinia(createPinia())
    const { default: realRequest } = await import('@/utils/request')
    const { dataApi: realDataApi } = await import('@/api/data')

    const replies = [
      {
        status: 202,
        body: { execution_id: 901, status: 'pending', message: 'Download queued' },
      },
      {
        status: 200,
        body: { execution_id: 902, status: 'pending', message: 'Unexpected status' },
      },
    ]
    const seen: Array<{ url: string; method: string; data: unknown }> = []
    const adapter: AxiosAdapter = async (config) => {
      const reply = replies.shift()!
      const response = {
        data: reply.body,
        status: reply.status,
        statusText: reply.status === 202 ? 'Accepted' : 'OK',
        headers: {},
        config,
        request: {},
      } as AxiosResponse
      seen.push({
        url: `${config.baseURL ?? ''}${config.url ?? ''}`,
        method: (config.method ?? 'get').toLowerCase(),
        data: config.data,
      })

      if (config.validateStatus && !config.validateStatus(reply.status)) {
        throw new AxiosError(
          `Request failed with status code ${reply.status}`,
          AxiosError.ERR_BAD_REQUEST,
          config,
          {},
          response
        )
      }
      return response
    }

    const originalAdapter = realRequest.defaults.adapter
    realRequest.defaults.adapter = adapter
    try {
      const result = await realDataApi.download(91, { symbol: '000001' })

      expect(result).toEqual({
        execution_id: 901,
        status: 'pending',
        message: 'Download queued',
      })
      expect(seen[0]).toMatchObject({
        url: '/api/v1/data/download',
        method: 'post',
      })
      expect(JSON.parse(seen[0].data as string)).toEqual({
        interface_id: 91,
        parameters: { symbol: '000001' },
      })

      await expect(realDataApi.download(91, {})).rejects.toThrow('status code 200')
      expect(seen).toHaveLength(2)
    } finally {
      realRequest.defaults.adapter = originalAdapter
    }
  })
})
