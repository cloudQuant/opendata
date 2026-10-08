import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import {
  catalogApi,
  type CatalogCapability,
  type CatalogEntry,
  type DataPage,
} from '@/api/catalog'
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
    markets: ['cn'],
    capabilities: [
      {
        asset_class: 'equity',
        domain: 'stock_daily',
        period: '1D',
        market: 'cn',
        source: 'akshare',
        verified: true,
        notes: '',
        callable: {
          module: 'opendata.data.providers.akshare.models.stock_daily',
          name: 'AkshareStockDailyFetcher.fetch',
        },
        endpoint: {
          name: 'query_domain_data',
          method: 'GET',
          path: '/api/v1/data/equity/stock_daily',
          query_filters: { source: 'akshare', period: '1D' },
        },
        parameters: [{ name: 'symbol', type: 'str', required: true, description: null }],
      },
    ],
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
    markets: ['eu', 'global'],
    capabilities: [
      {
        asset_class: 'economy',
        domain: 'economy_cpi',
        period: '1M',
        market: 'eu',
        source: 'ecb',
        verified: true,
        notes: '',
        callable: { module: 'opendata.data.providers.ecb.models.cpi', name: 'EcbCpiFetcher.fetch' },
        endpoint: {
          name: 'query_domain_data',
          method: 'GET',
          path: '/api/v1/data/economy/economy_cpi',
          query_filters: { source: 'ecb', period: '1M' },
        },
        parameters: [{ name: 'series', type: 'str', required: true, description: null }],
      },
      {
        asset_class: 'economy',
        domain: 'economy_cpi',
        period: '1M',
        market: 'global',
        source: 'imf',
        verified: false,
        notes: '',
        callable: { module: 'opendata.data.providers.imf.models.cpi', name: 'ImfCpiFetcher.fetch' },
        endpoint: {
          name: 'query_domain_data',
          method: 'GET',
          path: '/api/v1/data/economy/economy_cpi',
          query_filters: { source: 'imf', period: '1M' },
        },
        parameters: [{ name: 'series', type: 'str', required: true, description: null }],
      },
    ],
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
    markets: [],
    capabilities: [
      {
        asset_class: 'equity',
        domain: 'fund_action',
        period: 'snapshot',
        market: '',
        source: 'ths',
        verified: false,
        notes: '',
        callable: {
          module: 'opendata.data.providers.ths.models.fund_action',
          name: 'ThsFundActionFetcher.fetch',
        },
        endpoint: {
          name: 'query_domain_data',
          method: 'GET',
          path: '/api/v1/data/equity/fund_action',
          query_filters: { source: 'ths', period: 'snapshot' },
        },
        parameters: [],
      },
    ],
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
  markets: ['cn', 'eu', 'global'],
  expected_data_date: '2026-09-22',
  domains_total: 3,
  source_legs_total: 4,
}

const PENDING_ENDPOINT_CAPABILITY: CatalogCapability = {
  asset_class: 'equity',
  domain: 'c32_metadata_only_daily',
  period: '1D',
  market: 'cn',
  source: 'akshare',
  verified: true,
  notes: '',
  callable: {
    module: 'opendata.data.providers.akshare.models.stock_daily',
    name: 'AkshareStockDailyFetcher.fetch',
  },
  endpoint: null,
  parameters: [{ name: 'symbol', type: 'str', required: true, description: null }],
}

const PENDING_ENDPOINT_ENTRY: CatalogEntry = {
  domain: 'c32_metadata_only_daily',
  asset_class: 'equity',
  markets: ['cn'],
  capabilities: [PENDING_ENDPOINT_CAPABILITY],
  display_name: 'C32已注册日线接口',
  layer: null,
  table: null,
  freshness_field: null,
  latest: null,
  lag_days: null,
  status: 'unmapped',
  coverage: null,
  quality: null,
  sources: [
    {
      source: 'akshare',
      verified: true,
      table: null,
      status: 'unmapped',
      reason: 'domain_not_declared',
      latest: null,
      lag_days: null,
    },
  ],
  domain_defined: false,
  service_state: 'metadata_only',
  reason: 'domain_not_declared',
}

const PROVIDER_QUERY_ENTRIES: CatalogEntry[] = [
  {
    domain: 'fred_search',
    asset_class: 'macro',
    markets: ['us'],
    capabilities: [
      {
        asset_class: 'macro',
        domain: 'fred_search',
        period: 'snapshot',
        market: 'us',
        source: 'fred',
        verified: false,
        notes: '',
        callable: {
          module: 'opendata.data.providers.fred.models.search',
          name: 'FredSearchFetcher.fetch',
        },
        endpoint: null,
        model_query_endpoint: {
          model: 'FredSearch',
          method: 'POST',
          path: '/api/v1/providers/fred/models/FredSearch/query',
        },
        parameters: [{ name: 'search_text', type: 'str', required: true, description: null }],
      },
    ],
    display_name: 'FRED 系列目录检索',
    layer: null,
    table: null,
    freshness_field: null,
    latest: null,
    lag_days: null,
    status: 'unmapped',
    coverage: null,
    quality: null,
    sources: [
      {
        source: 'fred',
        verified: false,
        table: null,
        status: 'unmapped',
        reason: 'transient_model',
        latest: null,
        lag_days: null,
      },
    ],
    domain_defined: true,
    service_state: 'provider_query',
    reason: 'transient_model',
  },
  {
    domain: 'equity_historical',
    asset_class: 'stock',
    markets: ['us'],
    capabilities: [
      {
        asset_class: 'stock',
        domain: 'equity_historical',
        period: '1d',
        market: 'us',
        source: 'fmp',
        verified: false,
        notes: '',
        callable: {
          module: 'opendata.data.providers.fmp.models.equity_historical',
          name: 'EquityHistoricalFetcher.fetch',
        },
        endpoint: null,
        model_query_endpoint: {
          model: 'EquityHistorical',
          method: 'POST',
          path: '/api/v1/providers/fmp/models/EquityHistorical/query',
        },
        parameters: [{ name: 'symbol', type: 'str', required: true, description: null }],
      },
    ],
    display_name: '美股历史行情',
    layer: null,
    table: null,
    freshness_field: null,
    latest: null,
    lag_days: null,
    status: 'unmapped',
    coverage: null,
    quality: null,
    sources: [
      {
        source: 'fmp',
        verified: false,
        table: null,
        status: 'unmapped',
        reason: 'warehouse_not_ready',
        latest: null,
        lag_days: null,
      },
    ],
    domain_defined: true,
    service_state: 'provider_query',
    reason: 'warehouse_not_ready',
  },
]

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
    expect(catalog.markets).toEqual(['cn', 'eu', 'global'])
    expect(catalog.domains[0].capabilities[0].callable.name).toBe('AkshareStockDailyFetcher.fetch')
    const endpoint = catalog.domains[0].capabilities[0].endpoint
    expect(endpoint).not.toBeNull()
    expect(endpoint?.query_filters).toEqual({
      source: 'akshare',
      period: '1D',
    })
    expect(catalog.domains[0].capabilities[0].parameters[0].name).toBe('symbol')
  })

  it('returns an empty catalog when the payload is missing', async () => {
    get.mockResolvedValue(undefined)

    expect(await catalogApi.catalog()).toEqual({
      domains: [],
      markets: [],
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

  it('groups by registered market and labels missing market metadata explicitly', async () => {
    get.mockResolvedValue(CATALOG)
    const wrapper = mount(DataCatalogView)

    await flushPromises()

    const groups = wrapper.findAll('[data-testid="market-group"]')
    expect(groups.map(flat)).toHaveLength(4)
    expect(flat(groups[0])).toContain('市场：cn')
    expect(flat(groups[0])).toContain('A股日线行情')
    expect(flat(groups[1])).toContain('市场：eu')
    expect(flat(groups[1])).toContain('宏观CPI')
    expect(flat(groups[2])).toContain('市场：global')
    expect(flat(groups[2])).toContain('宏观CPI')
    expect(flat(groups[3])).toContain('市场：未知市场')
    expect(flat(wrapper)).toContain('不按市场拆分')
  })

  it('opens registered provider callable, endpoint, and parameter metadata', async () => {
    get.mockResolvedValue(CATALOG)
    const wrapper = mount(DataCatalogView)

    await flushPromises()
    await wrapper.find('[data-testid="registered-functions"]')!.trigger('click')
    await flushPromises()

    const dialog = flat(wrapper)
    expect(dialog).toContain('AkshareStockDailyFetcher.fetch')
    expect(dialog).toContain('GET /api/v1/data/equity/stock_daily')
    expect(dialog).toContain('source=akshare · period=1D')
    expect(dialog).toContain('symbol: str（必填）')
    expect(dialog).toContain('不会触发 Provider 抓取')
  })

  it('shows native provider-model routes separately from unavailable warehouse reads', async () => {
    get.mockResolvedValue({
      ...CATALOG,
      domains: [...CATALOG.domains, ...PROVIDER_QUERY_ENTRIES],
      markets: [...CATALOG.markets, 'us'],
      domains_total: 5,
      source_legs_total: 6,
    })
    const wrapper = mount(DataCatalogView)
    await flushPromises()

    const rowFor = (domain: string) =>
      wrapper.findAll('tbody tr').find((row) => flat(row).includes(domain))!
    const fredRow = rowFor('fred_search')
    expect(flat(fredRow)).toContain('fred 未映射 · 未验证')
    expect(fredRow.find('.leg-tag').attributes('title')).toBe('transient_model')
    await fredRow.find('[data-testid="registered-functions"]').trigger('click')
    await flushPromises()

    let dialog = flat(wrapper)
    expect(dialog).toContain('POST /api/v1/providers/fred/models/FredSearch/query')
    expect(dialog).toContain('原生模型：FredSearch')
    expect(dialog).toContain('仓库读取尚未就绪（当前仅提供源模型查询入口）')
    expect(dialog).not.toContain('GET /api/v1/data/macro/fred_search')
    expect(dialog).toContain('许可约束')

    const historicalRow = rowFor('equity_historical')
    expect(flat(historicalRow)).toContain('fmp 未映射 · 未验证')
    expect(historicalRow.find('.leg-tag').attributes('title')).toBe('warehouse_not_ready')
    await historicalRow.find('[data-testid="registered-functions"]').trigger('click')
    await flushPromises()

    dialog = flat(wrapper)
    expect(dialog).toContain('POST /api/v1/providers/fmp/models/EquityHistorical/query')
    expect(dialog).toContain('原生模型：EquityHistorical')
    expect(dialog).toContain('仓库读取尚未就绪（当前仅提供源模型查询入口）')
    expect(dialog).not.toContain('GET /api/v1/data/stock/equity_historical')
    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/data/catalog')
  })

  it('shows registered metadata when its domain has no data-service endpoint', async () => {
    get.mockResolvedValue({
      ...CATALOG,
      domains: [...CATALOG.domains, PENDING_ENDPOINT_ENTRY],
      domains_total: 4,
    })
    const wrapper = mount(DataCatalogView)

    await flushPromises()

    const pendingRow = wrapper
      .findAll('tbody tr')
      .find((row) => flat(row).includes('c32_metadata_only_daily'))!
    const pendingReadings = pendingRow.findAll('td').map(flat)
    expect(pendingReadings[0]).toBe('C32已注册日线接口')
    expect(pendingReadings[3]).toBe('—')
    expect(pendingReadings[4]).toBe('—')
    expect(pendingReadings[5]).toContain('未映射')
    expect(pendingReadings[6]).toContain('akshare 未映射 · 已验证')
    expect(pendingReadings[7]).toBe('—')
    const sourceTag = pendingRow.findAll('.leg-tag').find((tag) => flat(tag).includes('akshare'))
    expect(sourceTag?.attributes('title')).toBe('domain_not_declared')
    expect(flat(sourceTag!)).toContain('akshare 未映射 · 已验证')

    const registeredFunctions = wrapper.findAll('[data-testid="registered-functions"]')
    await registeredFunctions[1].trigger('click')
    await flushPromises()

    const dialog = flat(wrapper)
    expect(dialog).toContain('AkshareStockDailyFetcher.fetch')
    expect(dialog).toContain('akshare')
    expect(dialog).toContain('cn')
    expect(dialog).toContain('symbol: str（必填）')
    expect(dialog).toContain('数据服务尚未接通')
    expect(dialog).not.toContain('/api/v1/data/equity/c32_metadata_only_daily')
    expect(dialog).not.toContain('source=akshare · period=1D')
  })

  it('keeps "not measured" apart from "nothing" and apart from "clean"', async () => {
    get.mockResolvedValue(CATALOG)
    const wrapper = mount(DataCatalogView)

    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    const cpi = rows
      .find((row) => row.findAll('td').map(flat)[1] === 'economy_cpi')!
      .findAll('td')
      .map(flat)
    expect(cpi[0]).toBe('宏观CPI')
    // An empty table is a measured 0 rows, not a dash.
    expect(cpi[3]).toBe('0 行 · —')
    expect(cpi[4]).toBe('—') // 时间范围：无行可取 MIN/MAX
    expect(cpi[5]).toContain('缺失')
    expect(cpi[6]).toContain('fred 未映射')
    expect(cpi[7]).toBe('未测量') // 表在，但没有 `_diff_flag` 列可测

    const fundAction = rows
      .find((row) => row.findAll('td').map(flat)[1] === 'fund_action')!
      .findAll('td')
      .map(flat)
    expect(fundAction[0]).toBe('基金分红')
    expect(fundAction[3]).toBe('—') // 表不存在：连 0 行都不是
    expect(fundAction[7]).toBe('—')
    expect(fundAction[6]).toContain('ths 缺失')
    // 未映射 leg carries why on the tag itself, or the reader cannot tell a
    // missing field mapping from a missing table.
    expect(
      rows
        .find((row) => row.findAll('td').map(flat)[1] === 'economy_cpi')!
        .findAll('td')[6]
        .find('[title]')
        .attributes('title')
    ).toBe("unknown source 'fred'; mappings available: ['akshare', 'ths']")
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

  it('does not let an older preview response replace a newer selection', async () => {
    let resolveFirst!: (page: DataPage) => void
    let resolveSecond!: (page: DataPage) => void
    get.mockResolvedValueOnce(CATALOG)
    get
      .mockReturnValueOnce(
        new Promise<DataPage>((resolve) => {
          resolveFirst = resolve
        })
      )
      .mockReturnValueOnce(
        new Promise<DataPage>((resolve) => {
          resolveSecond = resolve
        })
      )
    const wrapper = mount(DataCatalogView)

    await flushPromises()
    const previewButtons = wrapper.findAll('button').filter((button) => button.text() === '预览')
    await previewButtons[0].trigger('click')
    await previewButtons[1].trigger('click')

    resolveSecond({
      ...PAGE,
      domain: 'economy_cpi',
      asset_class: 'economy',
      columns: ['marker'],
      rows: [{ marker: 'newer' }],
    })
    await flushPromises()
    resolveFirst({ ...PAGE, rows: [{ close: 'stale' }] })
    await flushPromises()

    expect(flat(wrapper)).toContain('宏观CPI（economy_cpi）')
    expect(flat(wrapper)).toContain('newer')
    expect(flat(wrapper)).not.toContain('stale')
  })
})
