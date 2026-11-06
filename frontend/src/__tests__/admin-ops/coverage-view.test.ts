import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { CatalogEntry, CatalogPayload } from '@/api/catalog'
import CoverageView from '@/views/admin/CoverageView.vue'

const apiMocks = vi.hoisted(() => ({ catalog: vi.fn(), domainFreshness: vi.fn() }))

vi.mock('@/api/coverage', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/coverage')>()
  return { ...actual, coverageApi: apiMocks }
})

import { useAuthStore } from '@/stores/auth'

/** Element Plus keeps the template's indentation, so raw text is full of newlines. */
function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ')
}

const MEASURED: CatalogEntry = {
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
  coverage: { rows: 8123, symbols: 543, start: '2019-01-02', end: '2026-10-23', diff_flagged: 0 },
  quality: null,
  sources: [],
  service_state: 'warehouse',
}

const METADATA_ONLY: CatalogEntry = {
  ...MEASURED,
  domain: 'c32_metadata_only_daily',
  display_name: 'C32已注册日线接口',
  table: null,
  latest: null,
  lag_days: null,
  status: 'unmapped',
  coverage: null,
  domain_defined: false,
  service_state: 'metadata_only',
  reason: 'domain_not_declared',
}

const UNMEASURABLE: CatalogEntry = {
  ...MEASURED,
  domain: 'fund_action',
  display_name: '基金分红',
  table: 'dwd_fund_action',
  latest: null,
  lag_days: null,
  status: 'missing',
  coverage: null,
}

function payloadOf(domains: CatalogEntry[]): CatalogPayload {
  return {
    domains,
    markets: ['cn'],
    expected_data_date: '2026-10-24',
    domains_total: domains.length,
    source_legs_total: 0,
  }
}

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = {
    user_id: 1,
    email: 'admin@example.com',
    role: 'admin',
    created_at: '2026-10-24T00:00:00Z',
    updated_at: null,
  }
  auth.accessToken = 'test-token'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/login', component: { template: '<div />' } },
      { path: '/admin/coverage', component: CoverageView },
    ],
  })
  await router.push('/admin/coverage')
  await router.isReady()
  const wrapper = mount(CoverageView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper, auth, router }
}

/** Legend counters follow `verdictOrder`: PROVEN, PARTIAL, SOURCE_LIMITED, NOT_SUPPORTED, UNKNOWN. */
function legendCounts(items: Array<{ text(): string }>): string[] {
  return items.map((item) => flat(item))
}

beforeEach(() => {
  vi.resetAllMocks()
  apiMocks.catalog.mockResolvedValue(payloadOf([MEASURED, METADATA_ONLY, UNMEASURABLE]))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('admin coverage page', () => {
  it('reads the catalog once on mount and states each domain verdict distinctly', async () => {
    const { wrapper } = await mountPage()

    expect(apiMocks.catalog).toHaveBeenCalledTimes(1)

    const texts = wrapper.findAll('tbody tr').map(flat)
    expect(texts).toHaveLength(3)
    expect(
      texts.some((row) => row.includes('A股日线行情') && row.includes('已测得（PROVEN）'))
    ).toBe(true)
    expect(
      texts.some(
        (row) => row.includes('C32已注册日线接口') && row.includes('不支持（NOT_SUPPORTED）')
      )
    ).toBe(true)
    expect(texts.some((row) => row.includes('基金分红') && row.includes('未知（UNKNOWN）'))).toBe(
      true
    )
    // The legend counts the verdicts, so a reader can see none was invented.
    expect(legendCounts(wrapper.findAll('.legend-count'))).toEqual(['1', '0', '0', '1', '1'])
    wrapper.unmount()
  })

  it('keeps an unmeasurable domain at UNKNOWN and admits SOURCE_LIMITED is unreachable here', async () => {
    const { wrapper } = await mountPage()
    const fundRow = wrapper.findAll('tbody tr').find((row) => flat(row).includes('基金分红'))!

    expect(flat(fundRow)).toContain('服务端报告 missing')
    expect(flat(fundRow)).toContain('无法据此判断历史是否完整')
    expect(flat(fundRow)).not.toContain('已测得（PROVEN）')
    expect(legendCounts(wrapper.findAll('.legend-count'))[2]).toBe('0')
    expect(flat(wrapper)).toContain('本契约面不暴露源保留边界，因此当前不会给出该判据')
    wrapper.unmount()
  })

  it('states instrument-level evidence coverage as UNKNOWN with the missing operation named', async () => {
    const { wrapper } = await mountPage()
    const panel = wrapper.find('[data-testid="instrument-coverage-unknown"]')

    expect(flat(panel)).toContain('该判据在本契约面不可得，按 UNKNOWN 显示')
    expect(flat(wrapper)).toContain('标的级证据覆盖在本契约面不可得')
    expect(flat(wrapper)).toContain('/api/v2/datasets/{dataset_id}/coverage')
    wrapper.unmount()
  })

  it('shows no coverage rows and names the gap when the catalog read fails', async () => {
    // mockRejectedValueOnce: the persistent form is broken in vitest 4.0.18.
    apiMocks.catalog.mockRejectedValueOnce(new Error('boom'))
    const { wrapper } = await mountPage()

    expect(wrapper.find('[data-testid="coverage-unavailable"]').exists()).toBe(true)
    expect(flat(wrapper)).toContain('不会用别的来源或样例数据填补')
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)
    expect(flat(wrapper.find('[data-testid="coverage-no-rows"]'))).toContain('不是"覆盖完整"')
    expect(legendCounts(wrapper.findAll('.legend-count'))).toEqual(['0', '0', '0', '0', '0'])
    wrapper.unmount()
  })

  it('treats a zero-domain catalog as no verdicts rather than a fully covered system', async () => {
    apiMocks.catalog.mockResolvedValueOnce(payloadOf([]))
    const { wrapper } = await mountPage()

    expect(flat(wrapper)).toContain('目录返回零个域')
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)
    wrapper.unmount()
  })

  it('does not turn a failed freshness read into a coverage claim', async () => {
    apiMocks.domainFreshness.mockResolvedValueOnce({
      domain: 'stock_daily',
      source: null,
      field: null,
      latest: null,
      lag_days: null,
      status: '',
      expectedDataDate: null,
      error: '新鲜度读取失败；未用任何其它来源替代。',
    })
    const { wrapper } = await mountPage()

    await wrapper.get('input[aria-label="域名"]').setValue('stock_daily')
    const readButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('读取新鲜度'))!
    await readButton.trigger('click')
    await flushPromises()

    expect(apiMocks.domainFreshness).toHaveBeenCalledTimes(1)
    expect(flat(wrapper)).toContain('未用任何其它来源替代')
    wrapper.unmount()
  })
})
