import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import BackfillPlanningView from '@/views/admin/BackfillPlanningView.vue'

const transportMocks = vi.hoisted(() => ({
  request: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), patch: vi.fn() },
  catalog: vi.fn(),
  coverage: vi.fn(),
}))

// Nothing on this page may reach a server: the plan surface has no HTTP
// operation, so every client module is stubbed and the assertions below prove
// the page never touches one.
vi.mock('@/utils/request', () => ({ default: transportMocks.request }))
vi.mock('@/api/catalog', () => ({ catalogApi: { catalog: transportMocks.catalog } }))
vi.mock('@/api/coverage', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/coverage')>()
  return { ...actual, coverageApi: { catalog: transportMocks.coverage, domainFreshness: vi.fn() } }
})

function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ')
}

const VALID_PROPOSAL = {
  dataset: 'cn-equity-stock-daily',
  instrument: '600000.SH',
  start: '2026-01-01',
  end: '2026-04-01',
  maxWindowDays: '90',
  chunkBudget: '500',
}

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/admin/backfill-planning', component: BackfillPlanningView },
    ],
  })
  await router.push('/admin/backfill-planning')
  await router.isReady()
  const wrapper = mount(BackfillPlanningView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper }
}

type PageWrapper = Awaited<ReturnType<typeof mountPage>> extends { wrapper: infer W } ? W : never

async function fillProposal(
  wrapper: PageWrapper,
  values: Partial<typeof VALID_PROPOSAL> = {}
): Promise<void> {
  const merged = { ...VALID_PROPOSAL, ...values }
  await wrapper.get('input[aria-label="dataset_id"]').setValue(merged.dataset)
  await wrapper.get('input[aria-label="标的"]').setValue(merged.instrument)
  await wrapper.get('input[aria-label="窗口起"]').setValue(merged.start)
  await wrapper.get('input[aria-label="窗口止"]').setValue(merged.end)
  await wrapper.get('input[aria-label="单片窗口上限"]').setValue(merged.maxWindowDays)
  await wrapper.get('input[aria-label="分片预算"]').setValue(merged.chunkBudget)
}

function submitButtons(wrapper: PageWrapper): string[] {
  return wrapper.findAll('button').map((button) => button.text())
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('backfill planning page', () => {
  it('states the un-assembled plan service instead of issuing requests', async () => {
    const { wrapper } = await mountPage()

    expect(wrapper.find('[data-testid="backfill-not-assembled"]').exists()).toBe(true)
    expect(flat(wrapper)).toContain('当前部署没有任何补数 HTTP 操作')
    expect(flat(wrapper)).toContain('不可得（无接口）')
    expect(transportMocks.request.get).not.toHaveBeenCalled()
    expect(transportMocks.request.post).not.toHaveBeenCalled()
    expect(transportMocks.catalog).not.toHaveBeenCalled()
    expect(transportMocks.coverage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('offers no run action and names the unbounded retry-failed hazard', async () => {
    const { wrapper } = await mountPage()
    const labels = submitButtons(wrapper)

    expect(labels.some((label) => label.includes('开始补数'))).toBe(false)
    expect(labels.some((label) => label.includes('执行'))).toBe(false)
    expect(labels.some((label) => label.includes('仅校验提案范围'))).toBe(true)
    expect(flat(wrapper.find('[data-testid="backfill-no-run-note"]'))).toContain(
      'POST /api/v1/pipeline/retry-failed'
    )
    expect(flat(wrapper)).toContain('execution_authorized=false')
    wrapper.unmount()
  })

  it('refuses an incomplete subject and a reversed window before echoing anything', async () => {
    const { wrapper } = await mountPage()

    await fillProposal(wrapper, { instrument: '' })
    await wrapper.find('form').trigger('submit')
    expect(flat(wrapper)).toContain('必须同时给出 dataset_id 与标的标识')
    expect(wrapper.find('[data-testid="backfill-check"]').exists()).toBe(false)

    await fillProposal(wrapper, { instrument: '600000.SH', start: '2026-04-01', end: '2026-01-01' })
    await wrapper.find('form').trigger('submit')
    expect(flat(wrapper)).toContain('窗口必须满足 start < end')
    expect(transportMocks.request.get).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('refuses a window or budget beyond the bounds the code itself enforces', async () => {
    const { wrapper } = await mountPage()

    await fillProposal(wrapper, { start: '2015-01-01', end: '2026-01-01' })
    await wrapper.find('form').trigger('submit')
    expect(flat(wrapper)).toContain('超过代码上限 3660 天')

    await fillProposal(wrapper, { start: '2026-01-01', end: '2026-04-01', chunkBudget: '25001' })
    await wrapper.find('form').trigger('submit')
    expect(flat(wrapper)).toContain('分片预算必须在 1…25000 之间')
    wrapper.unmount()
  })

  it('echoes a bounded proposal and keeps every server-side answer unavailable', async () => {
    const { wrapper } = await mountPage()

    await fillProposal(wrapper)
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(flat(wrapper)).toContain('cn-equity-stock-daily × 600000.SH')
    expect(flat(wrapper)).toContain('2026-01-01 → 2026-04-01（90 天）')
    expect(flat(wrapper)).toContain('500 个分片')
    expect(wrapper.findAll('[data-testid="backfill-check"]')).toHaveLength(3)
    expect(wrapper.findAll('[data-testid="backfill-unavailable-field"]')).toHaveLength(5)
    expect(flat(wrapper)).toContain('候选分片数（candidate_chunks）：不可得（无接口）')
    expect(flat(wrapper)).toContain('本页没有"开始补数"按钮')
    expect(transportMocks.request.get).not.toHaveBeenCalled()
    expect(transportMocks.request.post).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
