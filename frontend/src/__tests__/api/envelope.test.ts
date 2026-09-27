import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import request from '@/utils/request'
import { catalogApi } from '@/api/catalog'
import { warehouseApi } from '@/api/tables'
import { pipelineApi } from '@/api/data'

// This file deliberately does NOT mock `@/utils/request`. The six dead read
// paths it covers were green for exactly one reason: the module under test was
// replaced by a stub that resolved the *pre-interceptor* wire shape, so the
// double `response.data?.data` unwrap looked correct. Here the real axios
// instance runs, and only the transport is stubbed — the answers carry the
// `{success, message, data}` envelope the backend actually sends.

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface Seen {
  url: string
  method: string
  params: unknown
}

const seen: Seen[] = []
let nextBody: WireEnvelope = { success: true, data: null }

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig
): Promise<AxiosResponse> => {
  seen.push({
    url: `${config.baseURL ?? ''}${config.url ?? ''}`,
    method: (config.method ?? 'get').toLowerCase(),
    params: config.params,
  })
  return {
    data: nextBody,
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

function answer(data: unknown): void {
  nextBody = { success: true, message: 'success', data }
}

const CATALOG = {
  domains: [
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
          verified: true,
          table: 'ods_stock_daily_ths',
          status: 'fresh',
          reason: null,
          latest: '2026-09-22',
          lag_days: 0,
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
      coverage: null,
      quality: null,
      sources: [
        {
          source: 'fred',
          verified: true,
          table: 'ods_economy_cpi_fred',
          status: 'unmapped',
          reason: "unknown source 'fred'; mappings available: ['akshare', 'ths']",
          latest: null,
          lag_days: null,
        },
      ],
    },
  ],
  expected_data_date: '2026-09-22',
  domains_total: 2,
  source_legs_total: 3,
}

const PAGE = {
  domain: 'stock_daily',
  asset_class: 'equity',
  layer: 'dwd',
  source: 'ths',
  adjust: 'qfq',
  columns: ['symbol', 'trade_date', 'close'],
  rows: [{ symbol: '600519', trade_date: '2026-09-22', close: 1253.8 }],
  page: 1,
  page_size: 20,
  count: 3312,
}

const WAREHOUSE_TABLES = [
  {
    table: 'dwd_stock_daily',
    layer: 'dwd',
    domain: 'stock_daily',
    source: 'ths',
    rows: 8123456,
    size_mb: 612.4,
  },
  {
    table: 'dwd_economy_cpi',
    layer: 'dwd',
    domain: 'economy_cpi',
    source: 'fred',
    rows: 486,
    size_mb: 0.3,
  },
]

const FAILED_SHARD = {
  pipeline_id: 'stock_daily:2026-09-01',
  domain: 'stock_daily',
  source: 'ths',
  shard: 7,
  window: { start: '2026-09-01', end: '2026-09-22' },
  error: 'upstream timeout',
}

const originalAdapter = request.defaults.adapter

beforeAll(() => {
  request.defaults.adapter = adapter
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
})

beforeEach(() => {
  setActivePinia(createPinia())
  seen.length = 0
  nextBody = { success: true, data: null }
})

describe('catalogApi over the real interceptor', () => {
  it('reads the catalog payload out of the envelope', async () => {
    answer(CATALOG)

    const catalog = await catalogApi.catalog()

    expect(seen[0]).toEqual({ url: '/api/v1/data/catalog', method: 'get', params: undefined })
    // The payload arrives whole: the calendar baseline is as much a reading
    // as the domain rows are, and unwrapping only `domains` would drop it.
    expect(catalog.expected_data_date).toBe('2026-09-22')
    expect(catalog.domains).toHaveLength(2)
    expect(catalog.domains[0].display_name).toBe('A股日线行情')
    expect(catalog.domains[1].status).toBe('missing')
    expect(catalog.domains[0].sources[1].latest).toBe('2026-09-22')
  })

  it('reads a catalog with no domain as empty, keeping its baseline', async () => {
    // Reachable: an API key whose scopes cover no domain gets `domains: []`
    // with the baseline still set (test_data_catalog pins the same reading).
    answer({
      domains: [],
      expected_data_date: '2026-09-22',
      domains_total: 0,
      source_legs_total: 0,
    })

    const catalog = await catalogApi.catalog()

    expect(catalog.domains).toEqual([])
    expect(catalog.expected_data_date).toBe('2026-09-22')
    expect(catalog.domains_total).toBe(0)
  })

  it('reads a domain query as a whole DataPage, not an envelope', async () => {
    answer(PAGE)

    const page = await catalogApi.query('equity', 'stock_daily', { page: 2, page_size: 20 })

    expect(seen[0].url).toBe('/api/v1/data/equity/stock_daily')
    expect(seen[0].params).toEqual({ page: 2, page_size: 20 })
    expect(page.count).toBe(3312)
    expect(page.columns).toContain('close')
    expect(page.rows[0].close).toBe(1253.8)
    expect(page.adjust).toBe('qfq')
  })
})

describe('warehouseApi over the real interceptor', () => {
  it('reads the table list and puts `layer` on the wire', async () => {
    answer({ count: 2, tables: WAREHOUSE_TABLES })

    const tables = await warehouseApi.list('dwd')

    expect(seen[0]).toEqual({
      url: '/api/v1/tables/warehouse',
      method: 'get',
      params: { layer: 'dwd' },
    })
    expect(tables.map((t) => t.table)).toEqual(['dwd_stock_daily', 'dwd_economy_cpi'])
    expect(tables[0].rows).toBe(8123456)
  })
})

describe('pipelineApi over the real interceptor', () => {
  it('reads the failure list with its shard fields', async () => {
    answer({ count: 1, failures: [FAILED_SHARD] })

    const res = await pipelineApi.failures({ domain: 'stock_daily', limit: 50 })

    expect(seen[0].url).toBe('/api/v1/pipeline/failures')
    expect(seen[0].params).toEqual({ domain: 'stock_daily', limit: 50 })
    expect(res.count).toBe(1)
    expect(res.failures[0].shard).toBe(7)
    expect(res.failures[0].window.end).toBe('2026-09-22')
  })

  it('reads the reset count from the retry POST', async () => {
    answer({ reset: 3 })

    const res = await pipelineApi.retryFailed()

    expect(seen[0].method).toBe('post')
    expect(seen[0].url).toBe('/api/v1/pipeline/retry-failed')
    expect(res.reset).toBe(3)
  })
})
