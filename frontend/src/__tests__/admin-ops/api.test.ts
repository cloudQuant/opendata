import { beforeEach, describe, expect, it, vi } from 'vitest'
import { coverageApi } from '@/api/coverage'

// The API modules go through the shared axios instance, whose interceptor
// unwraps the envelope before callers see anything; the stub therefore resolves
// the payload inside `data`, exactly like src/__tests__/catalog.test.ts does.
vi.mock('@/utils/request', () => {
  const get = vi.fn()
  return { default: { get }, __get: get }
})

import request from '@/utils/request'

const get = (request as unknown as { get: ReturnType<typeof vi.fn> }).get

function resolved(payload: unknown): void {
  get.mockResolvedValue(payload)
}

beforeEach(() => {
  get.mockReset()
})

describe('coverage client wire contract', () => {
  it('reads the domain freshness through the one registered /api/v1 operation', async () => {
    const signal = new AbortController().signal
    resolved({
      domain: 'stock_daily',
      source: null,
      field: 'trade_date',
      latest: '2026-10-23',
      lag_days: 1,
      status: 'stale',
      expected_data_date: '2026-10-24',
    })

    const reading = await coverageApi.domainFreshness('stock_daily', {}, signal)

    expect(get).toHaveBeenCalledTimes(1)
    expect(get.mock.calls[0][0]).toBe('/data/domains/stock_daily/freshness')
    expect(get.mock.calls[0][1]).toEqual({ params: {}, signal })
    expect(reading).toMatchObject({ domain: 'stock_daily', status: 'stale', error: null })
  })

  it('keeps the no-data answer as its own state instead of inventing a date', async () => {
    // data_query.py answers this shape: no `field`, no `lag_days` at all.
    resolved({
      domain: 'stock_daily',
      source: null,
      status: 'missing',
      latest: null,
      expected_data_date: '2026-10-24',
    })

    const reading = await coverageApi.domainFreshness('stock_daily')

    expect(reading).toEqual({
      domain: 'stock_daily',
      source: null,
      field: null,
      latest: null,
      lag_days: null,
      status: 'missing',
      expectedDataDate: '2026-10-24',
      error: null,
    })
  })

  it('refuses a non-object payload as UNKNOWN rather than reading an empty object as coverage', async () => {
    resolved(null)

    const reading = await coverageApi.domainFreshness('stock_daily')

    expect(reading.status).toBe('')
    expect(reading.error).toContain('按 UNKNOWN 处理')
    expect(reading.latest).toBeNull()
  })

  it('validates the domain before building a path, so nothing can reach another plane', async () => {
    const attempts = [
      '/api/v2/datasets/x/coverage',
      '../v2/datasets/x/coverage',
      'stock daily',
      'STOCK_DAILY/../other',
      '',
    ]

    for (const domain of attempts) {
      await expect(coverageApi.domainFreshness(domain)).rejects.toThrow('coverage_domain_invalid')
    }

    expect(get).not.toHaveBeenCalled()
  })

  it('states a failed read in place and never falls back to another source', async () => {
    // mockRejectedValueOnce, not mockRejectedValue: the persistent form is
    // broken in vitest 4.0.18 (every call is flagged unhandled).
    get.mockRejectedValueOnce(new Error('boom'))

    const reading = await coverageApi.domainFreshness('stock_daily')

    expect(get).toHaveBeenCalledTimes(1)
    expect(reading.status).toBe('')
    expect(reading.error).toContain('未用任何其它来源替代')
    expect(reading.latest).toBeNull()
  })

  it('reads the catalog on the shipped plane only', async () => {
    resolved({
      domains: [],
      markets: [],
      expected_data_date: '',
      domains_total: 0,
      source_legs_total: 0,
    })

    await coverageApi.catalog()

    expect(get).toHaveBeenCalledTimes(1)
    expect(get.mock.calls[0][0]).toBe('/data/catalog')
    for (const call of get.mock.calls) {
      expect(String(call[0])).not.toMatch(/^\/api\/v2/)
      expect(String(call[0])).not.toMatch(/^\/reviewed/)
    }
  })
})
