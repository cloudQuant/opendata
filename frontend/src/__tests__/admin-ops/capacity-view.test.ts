import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import CapacityGateView from '@/views/admin/CapacityGateView.vue'

const transportMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/utils/request', () => ({ default: transportMocks }))

function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ')
}

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/admin/capacity-gate', component: CapacityGateView },
    ],
  })
  await router.push('/admin/capacity-gate')
  await router.isReady()
  const wrapper = mount(CapacityGateView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('capacity gate page', () => {
  it('states that the gate state is not observable and issues no request to guess it', async () => {
    const wrapper = await mountPage()

    expect(wrapper.find('[data-testid="capacity-not-observable"]').exists()).toBe(true)
    expect(flat(wrapper)).toContain('没有任何 HTTP 操作返回容量闸门状态')
    expect(flat(wrapper)).toContain('"关闭"和"pin 无效"在设计上不可区分')
    expect(transportMocks.get).not.toHaveBeenCalled()
    expect(transportMocks.post).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('reports the gate as default-off with its fail-closed conditions', async () => {
    const wrapper = await mountPage()

    expect(flat(wrapper)).toContain('默认关闭（default-off）')
    expect(flat(wrapper)).toContain('STOCK_DAILY_PUBLICATION_API_ENABLED 恰为字面 true')
    expect(flat(wrapper)).toContain('STOCK_DAILY_PUBLICATION_CAPACITY_FILE')
    expect(flat(wrapper)).toContain('STOCK_DAILY_PUBLICATION_CAPACITY_SHA256')
    expect(flat(wrapper)).toContain('not_before ≤ now < expires_at')
    expect(wrapper.findAll('[data-testid="capacity-gate"]')).toHaveLength(3)
    wrapper.unmount()
  })

  it('lists attestation field names without asserting any value', async () => {
    const wrapper = await mountPage()

    expect(flat(wrapper.find('[data-testid="capacity-no-values"]'))).toContain(
      '也不代替部署方声明任何摘要、日期或署名'
    )
    expect(flat(wrapper)).toContain('max_window_days（1…3660）')
    expect(flat(wrapper)).toContain('max_page_size（1…1000）')
    wrapper.unmount()
  })

  it('keeps the runtime state unknown instead of turning a 404 probe into a reading', async () => {
    const wrapper = await mountPage()

    expect(flat(wrapper.find('[data-testid="capacity-runtime-unknowns"]'))).toContain(
      '不可观察（无接口，且 404 折叠使探测无意义）'
    )

    const recheck = wrapper.findAll('button').find((button) => button.text().includes('重新核对'))!
    await recheck.trigger('click')
    await flushPromises()

    expect(transportMocks.get).not.toHaveBeenCalled()
    expect(transportMocks.post).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
