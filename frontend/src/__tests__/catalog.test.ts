import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { catalogApi, type DataPage } from '@/api/catalog'
import DataCatalogView from '@/views/DataCatalogView.vue'

// The API modules go through the shared axios instance; point it at a stub.
// The stub resolves what that instance hands back *after* its response
// interceptor, i.e. the payload inside the envelope — not the wire body. The
// interceptor itself is covered by src/__tests__/api/envelope.test.ts, which
// runs the real axios instance.
vi.mock('@/utils/request', () => {
  const get = vi.fn()
  return {
    default: { get },
    __get: get,
  }
})

import request from '@/utils/request'
const get = (request as unknown as { get: ReturnType<typeof vi.fn> }).get

const ENTRIES = [
  {
    domain: 'stock_daily',
    asset_class: 'equity',
    display_name: 'A股日线行情',
    layer: 'dwd',
    table: 'dwd_stock_daily',
    freshness_field: 'trade_date',
    latest: '2026-09-22',
    lag_days: 1,
    status: 'stale',
    coverage: {
      rows: 8123456,
      symbols: 5432,
      start: '2019-01-02',
      end: '2026-09-22',
      diff_flagged: 3,
    },
    quality: { diff_flagged: 3, diff_report_rows: 12, flag: 'flagged' },
    sources: [
      {
        source: 'akshare',
        verified: true,
        table: 'ods_stock_daily_akshare',
        status: 'stale',
        reason: null,
        latest: '2026-09-21',
        lag_days: 2,
      },
      {
        source: 'ths',
        verified: false,
        table: 'ods_stock_daily_ths',
        status: 'missing',
        reason: null,
        latest: null,
        lag_days: null,
      },
    ],
  },
  {
    domain: 'economy_cpi',
    asset_class: 'economy',
    display_name: '宏观CPI',
    layer: 'dwd',
    table: 'dwd_economy_cpi',
    freshness_field: 'date',
    latest: null,
    lag_days: null,
    status: 'missing',
    // The table is readable but carries no `_diff_flag` column, so the
    // quality dimension is unmeasured - not clean.
    coverage: { rows: 0, symbols: null, start: null, end: null, diff_flagged: null },
    quality: { diff_flagged: null, diff_report_rows: null, flag: 'unmeasured' },
    sources: [
      {
        source: 'fred',
        verified: false,
        table: 'ods_economy_cpi_fred',
        status: 'unmapped',
        reason: "unknown source 'fred'; mappings available: ['akshare', 'ths']",
        latest: null,
        lag_days: null,
      },
    ],
  },
  {
    domain: 'fund_action',
    asset_class: 'equity',
    display_name: '基金分红',
    layer: 'dwd',
    table: 'dwd_fund_action',
    // No table to read: coverage/quality/freshness_field are all null, which
    // the page must not print as 0 rows or as 一致.
    freshness_field: null,
    latest: null,
    lag_days: null,
    status: 'missing',
    coverage: null,
    quality: null,
    sources: [
      {
        source: 'ths',
        verified: false,
        table: 'ods_fund_action_ths',
        status: 'missing',
        reason: null,
        latest: null,
        lag_days: null,
      },
    ],
  },
]

const CATALOG = {
  domains: ENTRIES,
  expected_data_date: '2026-09-22',
  domains_total: 3,
  source_legs_total: 4,
}

const PAGE: DataPage = {
  domain: 'stock_daily',
  asset_class: 'equity',
  layer: 'dwd',
  source: 'ths',
  adjust: 'none',
  columns: ['symbol', 'close'],
  rows: [{ symbol: '600519', close: 1253.8 }],
  page: 1,
  page_size: 20,
  count: 1,
}

describe('catalogApi', () => {
  beforeEach(() => get.mockReset())

  it('reads the whole catalog payload, not just the rows', async () => {
    get.mockResolvedValue(CATALOG)

    const catalog = await catalogApi.catalog()

    expect(get).toHaveBeenCalledWith('/data/catalog')
    expect(catalog.domains).toHaveLength(3)
    expect(catalog.domains[0].domain).toBe('stock_daily')
    expect(catalog.domains[0].status).toBe('stale')
    // The baseline every lag is measured against travels with the payload;
    // dropping it would leave "滞后 1 天" without a reference.
    expect(catalog.expected_data_date).toBe('2026-09-22')
    expect(catalog.source_legs_total).toBe(4)
  })

  it('returns an empty catalog when the payload is missing', async () => {
    get.mockResolvedValue(undefined)

    expect(await catalogApi.catalog()).toEqual({
      domains: [],
      expected_data_date: '',
      domains_total: 0,
      source_legs_total: 0,
    })
  })

  it('propagates a transport failure to the caller', async () => {
    // mockRejectedValueOnce, not mockRejectedValue: the persistent
    // form is broken in vitest 4.0.18 (every call is flagged unhandled).
    get.mockRejectedValueOnce(new Error('boom'))

    await expect(catalogApi.catalog()).rejects.toThrow('boom')
  })

  it('queries a domain with the given parameters', async () => {
    get.mockResolvedValue(PAGE)

    const page = await catalogApi.query('equity', 'stock_daily', { page_size: 20 })

    expect(get).toHaveBeenCalledWith('/data/equity/stock_daily', { params: { page_size: 20 } })
    expect(page.rows).toHaveLength(1)
    expect(page.rows[0].close).toBe(1253.8)
  })
})

/**
 * Element Plus puts every cell in its own element and keeps the template's
 * indentation, so raw textContent is full of newlines. Collapse before
 * matching a reading that spans more than one interpolation.
 */
function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ')
}

describe('DataCatalogView', () => {
  beforeEach(() => get.mockReset())

  it('states the baseline every lag is measured against', async () => {
    get.mockResolvedValue(CATALOG)
    const wrapper = mount(DataCatalogView)

    await flushPromises()

    // "滞后 1 天" is meaningless without the expected data date it came from,
    // and that date is a trading day, not today.
    expect(flat(wrapper)).toContain('基准日 2026-09-22 · 3 域 · 4 源腿')
  })

  it('renders the five readings for a measured domain', async () => {
    get.mockResolvedValue(CATALOG)
    const wrapper = mount(DataCatalogView)

    await flushPromises()

    const row = flat(wrapper.findAll('tbody tr')[0])
    expect(row).toContain('A股日线行情')
    expect(row).toContain('8123456 行 · 5432 标的') // 覆盖：行数来自表本身，标的数来自主键
    expect(row).toContain('2019-01-02 ~ 2026-09-22') // 时间范围
    expect(row).toContain('2026-09-22（滞后 1 天）') // 新鲜度
    expect(row).toContain('akshare 2026-09-21（滞后 2 天） · 已验证') // 各源最近更新
    expect(row).toContain('ths 缺失 · 未验证')
    expect(row).toContain('有差异') // 质量
  })

  it('keeps "not measured" apart from "nothing" and apart from "clean"', async () => {
    get.mockResolvedValue(CATALOG)
    const wrapper = mount(DataCatalogView)

    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    const cpi = rows[1].findAll('td').map(flat)
    expect(cpi[0]).toBe('宏观CPI')
    // An empty table is a measured 0 rows, not a dash.
    expect(cpi[3]).toBe('0 行 · —')
    expect(cpi[4]).toBe('—') // 时间范围：无行可取 MIN/MAX
    expect(cpi[5]).toContain('缺失')
    expect(cpi[6]).toContain('fred 未映射')
    expect(cpi[7]).toBe('未测量') // 表在，但没有 `_diff_flag` 列可测

    const fundAction = rows[2].findAll('td').map(flat)
    expect(fundAction[0]).toBe('基金分红')
    expect(fundAction[3]).toBe('—') // 表不存在：连 0 行都不是
    expect(fundAction[7]).toBe('—')
    expect(fundAction[6]).toContain('ths 缺失')
    // 未映射 leg carries why on the tag itself, or the reader cannot tell a
    // missing field mapping from a missing table.
    expect(rows[1].findAll('td')[6].find('[title]').attributes('title')).toBe(
      "unknown source 'fred'; mappings available: ['akshare', 'ths']"
    )
  })

  it('drills into a domain preview on demand', async () => {
    get.mockResolvedValueOnce(CATALOG)
    get.mockResolvedValueOnce(PAGE)
    const wrapper = mount(DataCatalogView)

    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '预览')
      ?.trigger('click')
    await flushPromises()

    expect(get).toHaveBeenLastCalledWith('/data/equity/stock_daily', {
      params: { page: 1, page_size: 20 },
    })
    const dialog = flat(wrapper)
    expect(dialog).toContain('1253.8')
    // A row is a domain now, not a (domain, source) leg: the drill-down names
    // the merged table it read rather than a single source.
    expect(dialog).toContain('共 1 行（最近 20 行预览）· 表 dwd_stock_daily')
  })
})
