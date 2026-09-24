import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { catalogApi } from '@/api/catalog'
import DataCatalogView from '@/views/DataCatalogView.vue'

// The API modules go through the shared axios instance; point it at a stub.
vi.mock('@/utils/request', () => {
  const get = vi.fn()
  return {
    default: { get },
    __get: get,
  }
})

import request from '@/utils/request'
const get = (request as unknown as { get: ReturnType<typeof vi.fn> }).get

function envelope(data: unknown) {
  return { data: { success: true, message: 'success', data } }
}

const ENTRIES = [
  {
    domain: 'stock_daily',
    asset_class: 'equity',
    source: 'ths',
    verified: true,
    display_name: 'A股日线行情',
    layer: 'dwd',
    latest: '2026-09-22',
    lag_days: 1,
    status: 'stale',
  },
  {
    domain: 'economy_cpi',
    asset_class: 'economy',
    source: 'fred',
    verified: true,
    display_name: '宏观CPI',
    layer: 'dwd',
    latest: null,
    lag_days: null,
    status: 'missing',
  },
]

describe('catalogApi', () => {
  beforeEach(() => get.mockReset())

  it('unwraps the catalog envelope into domain entries', async () => {
    get.mockResolvedValue(envelope({ domains: ENTRIES }))

    const entries = await catalogApi.catalog()

    expect(get).toHaveBeenCalledWith('/data/catalog')
    expect(entries).toHaveLength(2)
    expect(entries[0].domain).toBe('stock_daily')
    expect(entries[0].status).toBe('stale')
  })

  it('returns an empty list when the payload is missing', async () => {
    get.mockResolvedValue({ data: { success: true, data: {} } })

    expect(await catalogApi.catalog()).toEqual([])
  })

  it('propagates a transport failure to the caller', async () => {
    // mockRejectedValueOnce, not mockRejectedValue: the persistent
    // form is broken in vitest 4.0.18 (every call is flagged unhandled).
    get.mockRejectedValueOnce(new Error('boom'))

    await expect(catalogApi.catalog()).rejects.toThrow('boom')
  })

  it('queries a domain with the given parameters', async () => {
    get.mockResolvedValue(
      envelope({
        rows: [{ symbol: '600519', close: 1253.8 }],
        columns: ['symbol', 'close'],
        page: 1,
        page_size: 20,
        count: 1,
      }),
    )

    const page = await catalogApi.query('equity', 'stock_daily', { page_size: 20 })

    expect(get).toHaveBeenCalledWith('/data/equity/stock_daily', { params: { page_size: 20 } })
    expect(page.rows).toHaveLength(1)
    expect(page.rows[0].close).toBe(1253.8)
  })
})

describe('DataCatalogView', () => {
  beforeEach(() => get.mockReset())

  it('renders the domains with their freshness state', async () => {
    get.mockResolvedValue(envelope({ domains: ENTRIES }))
    const wrapper = mount(DataCatalogView)

    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('A股日线行情')
    expect(text).toContain('stock_daily')
    expect(text).toContain('宏观CPI')
    expect(text).toContain('滞后')
    expect(text).toContain('缺失')
  })

  it('drills into a domain preview on demand', async () => {
    get.mockResolvedValueOnce(envelope({ domains: ENTRIES }))
    get.mockResolvedValueOnce(
      envelope({
        rows: [{ symbol: '600519', close: 1253.8 }],
        columns: ['symbol', 'close'],
        page: 1,
        page_size: 20,
        count: 1,
      }),
    )
    const wrapper = mount(DataCatalogView)

    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '预览')?.trigger('click')
    await flushPromises()

    expect(get).toHaveBeenLastCalledWith('/data/equity/stock_daily', { params: expect.any(Object) })
    expect(wrapper.text()).toContain('1253.8')
  })
})
