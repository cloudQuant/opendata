import { describe, expect, it } from 'vitest'
import {
  buildCoverageRows,
  classifyCatalogEntry,
  classifyCoverageToken,
  instrumentCoverageRow,
  presentationOf,
  VERDICT_PRESENTATION,
} from '@/api/coverage'
import type { CatalogEntry, CatalogPayload } from '@/api/catalog'

function entryWith(overrides: Partial<CatalogEntry>): CatalogEntry {
  return {
    domain: 'stock_daily',
    asset_class: 'equity',
    markets: ['cn'],
    capabilities: [],
    display_name: 'A股日线行情',
    layer: 'dwd',
    table: 'dwd_stock_daily',
    freshness_field: 'trade_date',
    latest: '2026-10-23',
    lag_days: 1,
    status: 'stale',
    coverage: { rows: 120, symbols: 4, start: '2026-01-02', end: '2026-10-23', diff_flagged: 0 },
    quality: null,
    sources: [],
    ...overrides,
  }
}

function payloadWith(domains: CatalogEntry[]): CatalogPayload {
  return {
    domains,
    markets: ['cn'],
    expected_data_date: '2026-10-24',
    domains_total: domains.length,
    source_legs_total: 0,
  }
}

describe('coverage verdict classification', () => {
  it('maps every coverage token the product vocabulary uses, and fails closed', () => {
    expect(classifyCoverageToken('complete')).toBe('PROVEN')
    expect(classifyCoverageToken('partial')).toBe('PARTIAL')
    expect(classifyCoverageToken('source_limited')).toBe('SOURCE_LIMITED')
    expect(classifyCoverageToken('not_applicable')).toBe('NOT_SUPPORTED')
    expect(classifyCoverageToken('unknown')).toBe('UNKNOWN')
    // An absent or unrecognised token is never allowed to look complete.
    expect(classifyCoverageToken(null)).toBe('UNKNOWN')
    expect(classifyCoverageToken(undefined)).toBe('UNKNOWN')
    expect(classifyCoverageToken('definitely-not-a-verdict')).toBe('UNKNOWN')
  })

  it('states the five verdicts distinctly, with UNKNOWN grey and never green', () => {
    const verdicts = ['PROVEN', 'PARTIAL', 'SOURCE_LIMITED', 'NOT_SUPPORTED', 'UNKNOWN'] as const
    const labels = verdicts.map((verdict) => VERDICT_PRESENTATION[verdict].label)
    expect(new Set(labels).size).toBe(5)
    expect(VERDICT_PRESENTATION.UNKNOWN.tag).toBe('info')
    expect(VERDICT_PRESENTATION.PROVEN.tag).toBe('success')
    expect(presentationOf('nonsense').label).toBe(VERDICT_PRESENTATION.UNKNOWN.label)
  })

  it('calls a measured non-empty window PROVEN and keeps a lagging window PROVEN too', () => {
    expect(classifyCatalogEntry(entryWith({}))).toMatchObject({ verdict: 'PROVEN' })
    expect(classifyCatalogEntry(entryWith({ status: 'fresh', lag_days: 0 }))).toMatchObject({
      verdict: 'PROVEN',
    })
  })

  it('renders an unmeasurable domain as UNKNOWN, never as complete', () => {
    const missing = classifyCatalogEntry(
      entryWith({ status: 'missing', coverage: null, latest: null, lag_days: null })
    )
    expect(missing.verdict).toBe('UNKNOWN')
    expect(missing.reason).toContain('missing')

    const noWindow = classifyCatalogEntry(
      entryWith({
        coverage: { rows: 0, symbols: null, start: null, end: null, diff_flagged: null },
      })
    )
    expect(noWindow.verdict).toBe('UNKNOWN')
    expect(noWindow.reason).toContain('空窗口不等于覆盖完整')
  })

  it('does not let a window without a measurable row count become PROVEN', () => {
    const uncounted = classifyCatalogEntry(
      entryWith({
        coverage: {
          rows: 0,
          symbols: 0,
          start: '2026-01-02',
          end: '2026-02-02',
          diff_flagged: null,
        },
      })
    )
    expect(uncounted.verdict).toBe('UNKNOWN')
    expect(uncounted.reason).toContain('不能把它当作已证覆盖')
  })

  it('reports NOT_SUPPORTED only from the server-side service states, with the reason', () => {
    const metadataOnly = classifyCatalogEntry(
      entryWith({
        service_state: 'metadata_only',
        reason: 'domain_not_declared',
        status: 'unmapped',
        coverage: null,
      })
    )
    expect(metadataOnly.verdict).toBe('NOT_SUPPORTED')
    expect(metadataOnly.reason).toContain('domain_not_declared')

    const providerQuery = classifyCatalogEntry(
      entryWith({
        service_state: 'provider_query',
        reason: 'transient_model',
        status: 'unmapped',
        coverage: null,
      })
    )
    expect(providerQuery.verdict).toBe('NOT_SUPPORTED')
    expect(providerQuery.reason).toContain('transient_model')
  })

  it('keeps an unrecognised freshness token at UNKNOWN', () => {
    const odd = classifyCatalogEntry(
      entryWith({ status: 'synchronising' as unknown as CatalogEntry['status'] })
    )
    expect(odd.verdict).toBe('UNKNOWN')
    expect(odd.reason).toContain('synchronising')
  })
})

describe('coverage row building', () => {
  it('builds one row per catalog domain and names the reading it came from', () => {
    const rows = buildCoverageRows(
      payloadWith([
        entryWith({}),
        entryWith({
          domain: 'index_daily',
          display_name: '指数日线',
          service_state: 'metadata_only',
          reason: 'domain_not_declared',
          status: 'unmapped',
          coverage: null,
        }),
      ])
    )

    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({
      key: 'equity/stock_daily',
      verdict: 'PROVEN',
      measuredThrough: 'GET /api/v1/data/catalog',
    })
    expect(rows[1].verdict).toBe('NOT_SUPPORTED')
    expect(rows[1].observed.start).toBeNull()
  })

  it('names instrument-level evidence coverage UNKNOWN and cites the missing operation', () => {
    const row = instrumentCoverageRow('cn-equity-stock-daily', '600000.SH')
    expect(row.verdict).toBe('UNKNOWN')
    expect(row.subject).toBe('cn-equity-stock-daily × 600000.SH')
    expect(row.reason).toContain('/api/v2/datasets/{dataset_id}/coverage')
    expect(row.measuredThrough).toBe('（无接口调用）')
    expect(row.observed).toEqual({ start: null, end: null, rows: null, symbols: null })
    // The formatted readings say "not measured"; they never print a date or a zero.
    expect(row.windowText).toBe('—（窗口未测得）')
    expect(row.factsText).toBe('行数未测得 · 标的数未测得')
  })

  it('formats a measured window and leaves an unmeasured one visibly unmeasured', () => {
    const [measured, unmeasured] = buildCoverageRows(
      payloadWith([
        entryWith({}),
        entryWith({ status: 'missing', coverage: null, latest: null, lag_days: null }),
      ])
    )
    expect(measured.windowText).toBe('2026-01-02 → 2026-10-23')
    expect(measured.factsText).toBe('120 行 · 4 标的')
    expect(unmeasured.windowText).toBe('—（窗口未测得）')
    expect(unmeasured.factsText).not.toMatch(/\b0\b/)
  })
})
