/* eslint vue/one-component-per-file: off */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { mount, flushPromises, enableAutoUnmount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, type PropType, type VNode } from 'vue'
import request from '@/utils/request'
import HomeView from '@/views/HomeView.vue'

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface RequestCall {
  url: string
  method: string
  params: unknown
}

const calls: RequestCall[] = []
let respond: (call: RequestCall) => WireEnvelope | Promise<WireEnvelope> = (call) => {
  if (call.url === '/api/v1/executions/stats') {
    return {
      success: true,
      data: {
        total_count: 0,
        success_count: 0,
        failed_count: 0,
        success_rate: 0,
        avg_duration: 0,
        today_executions: 0,
      },
    }
  }
  return { success: true, data: { items: [], total: 0, page: 1, page_size: 5 } }
}
const originalAdapter = request.defaults.adapter

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig
): Promise<AxiosResponse> => {
  const call = {
    url: `${config.baseURL ?? ''}${config.url ?? ''}`,
    method: (config.method ?? 'get').toLowerCase(),
    params: config.params,
  }
  calls.push(call)
  return {
    data: await respond(call),
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function columnSlots(node: VNode, row: Record<string, unknown>, index: number) {
  const children = node.children as {
    default?: (scope: { row: unknown; $index: number }) => VNode[]
  } | null
  return children?.default?.({ row, $index: index })
}

const DestinationView = defineComponent({
  setup() {
    return () => h('div', { 'data-testid': 'destination-view' })
  },
})

const stubs = {
  ElCard: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => h('section', attrs, [slots.header?.(), slots.default?.()])
    },
  }),
  ElSkeleton: defineComponent({
    setup() {
      return () => h('div', { 'data-testid': 'loading-skeleton', role: 'status' })
    },
  }),
  ElIcon: defineComponent({
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  ElButton: defineComponent({
    inheritAttrs: false,
    props: {
      type: { type: String, default: undefined },
      size: { type: String, default: undefined },
      link: { type: Boolean, default: undefined },
      icon: { type: Object, default: undefined },
    },
    emits: ['click'],
    setup(_props, { attrs, slots, emit }) {
      return () =>
        h(
          'button',
          {
            ...attrs,
            type: 'button',
            onClick: (event: Event) => emit('click', event),
          },
          slots.default?.()
        )
    },
  }),
  ElAlert: defineComponent({
    props: { title: { type: String, default: '' } },
    setup(props, { slots }) {
      return () => h('aside', { role: 'alert' }, [h('span', props.title), slots.default?.()])
    },
  }),
  ElTable: defineComponent({
    props: {
      data: { type: Array as PropType<Record<string, unknown>[]>, default: () => [] },
    },
    setup(props, { slots }) {
      return () => {
        const columns = slots.default?.() ?? []
        return h(
          'table',
          { 'data-testid': 'recent-scripts' },
          h(
            'tbody',
            props.data.map((row, index) =>
              h(
                'tr',
                columns.map((column) => {
                  const content = columnSlots(column, row, index)
                  if (content) return h('td', content)
                  const prop = column.props?.prop as string | undefined
                  return h('td', String((prop ? row[prop] : '') ?? ''))
                })
              )
            )
          )
        )
      }
    },
  }),
  ElTableColumn: defineComponent({
    props: {
      prop: { type: String, default: undefined },
      label: { type: String, default: undefined },
    },
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  ElEmpty: defineComponent({
    props: { description: { type: String, default: '' } },
    setup(props) {
      return () => h('div', { 'data-testid': 'empty-state' }, props.description)
    },
  }),
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  calls.length = 0
  respond = (call) => {
    if (call.url === '/api/v1/executions/stats') {
      return {
        success: true,
        data: {
          total_count: 0,
          success_count: 0,
          failed_count: 0,
          success_rate: 0,
          avg_duration: 0,
          today_executions: 0,
        },
      }
    }
    return { success: true, data: { items: [], total: 0, page: 1, page_size: 5 } }
  }
  request.defaults.adapter = adapter
  setActivePinia(createPinia())
})

afterEach(() => {
  vi.restoreAllMocks()
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
})

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: HomeView },
      { path: '/scripts', component: DestinationView },
      { path: '/scripts/:id', component: DestinationView },
      { path: '/tasks', component: DestinationView },
      { path: '/tables', component: DestinationView },
    ],
  })
}

async function mountHome() {
  const router = makeRouter()
  await router.push('/')
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(HomeView, {
    global: {
      plugins: [pinia, router],
      stubs,
    },
  })
  return { wrapper, router }
}

function statsEnvelope(overrides: Record<string, number> = {}): WireEnvelope {
  return {
    success: true,
    data: {
      total_count: 20,
      success_count: 17,
      failed_count: 3,
      success_rate: 85,
      avg_duration: 1.25,
      today_executions: 4,
      ...overrides,
    },
  }
}

function scriptsEnvelope(items: unknown[]): WireEnvelope {
  return { success: true, data: { items, total: items.length, page: 1, page_size: 5 } }
}

function script(scriptId: string, name: string, category: string) {
  return {
    id: 1,
    script_id: scriptId,
    script_name: name,
    category,
    sub_category: null,
    frequency: null,
    description: null,
    source: 'market-data',
    target_table: null,
    module_path: null,
    function_name: null,
    estimated_duration: 1,
    timeout: 10,
    is_active: true,
    is_custom: false,
    created_at: '2026-10-08T00:00:00Z',
    updated_at: '2026-10-08T00:00:00Z',
  }
}

function findButton(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
  expect(button, `button named ${label}`).toBeDefined()
  return button!
}

describe('HomeView operator flow', () => {
  it('starts stats and recent-script requests together, shows loading, then renders both results', async () => {
    const statsGate = deferred<WireEnvelope>()
    const scriptsGate = deferred<WireEnvelope>()
    respond = (call) =>
      call.url === '/api/v1/executions/stats' ? statsGate.promise : scriptsGate.promise

    const { wrapper } = await mountHome()
    await flushPromises()

    expect(calls).toHaveLength(2)
    expect(calls).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          url: '/api/v1/executions/stats',
          method: 'get',
          params: undefined,
        }),
        expect.objectContaining({
          url: '/api/v1/scripts/',
          method: 'get',
          params: { page: 1, page_size: 5 },
        }),
      ])
    )
    expect(wrapper.findAll('[data-testid="loading-skeleton"]')).toHaveLength(4)
    expect(wrapper.text()).not.toContain('总执行次数')
    expect(wrapper.findAll('[data-testid="recent-scripts"] tbody tr')).toHaveLength(0)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)

    statsGate.resolve(statsEnvelope())
    await flushPromises()
    expect(wrapper.findAll('[data-testid="loading-skeleton"]')).toHaveLength(4)

    scriptsGate.resolve(
      scriptsEnvelope([
        script('market.daily', '日线行情', '行情'),
        script('market.fund_flow', '资金流向', '资金'),
      ])
    )
    await flushPromises()

    expect(wrapper.text()).toContain('20')
    expect(wrapper.text()).toContain('17')
    expect(wrapper.text()).toContain('3')
    expect(wrapper.text()).toContain('85.0%')
    expect(wrapper.text()).toContain('日线行情')
    expect(wrapper.text()).toContain('资金流向')
    expect(wrapper.findAll('[data-testid="loading-skeleton"]')).toHaveLength(0)
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(false)
  })

  it('shows the empty state when the recent-script endpoint returns no rows', async () => {
    respond = (call) =>
      call.url === '/api/v1/executions/stats' ? statsEnvelope() : scriptsEnvelope([])

    const { wrapper } = await mountHome()
    await flushPromises()

    expect(calls).toHaveLength(2)
    expect(calls.find((call) => call.url === '/api/v1/scripts/')).toMatchObject({
      params: { page: 1, page_size: 5 },
    })
    expect(wrapper.find('[data-testid="empty-state"]').text()).toContain('暂无最近使用的接口')
    expect(wrapper.findAll('[data-testid="recent-scripts"] tbody tr')).toHaveLength(0)
  })

  it('shows a request failure and retries the parallel dashboard load', async () => {
    let statsAttempts = 0
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    respond = (call) => {
      if (call.url === '/api/v1/executions/stats') {
        statsAttempts += 1
        return statsAttempts === 1
          ? { success: false, message: '统计暂不可用' }
          : statsEnvelope({ total_count: 9 })
      }
      return scriptsEnvelope([script('market.retry', '重试后接口', '测试')])
    }

    const { wrapper } = await mountHome()
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('统计暂不可用')
    expect(calls.filter((call) => call.url === '/api/v1/executions/stats')).toHaveLength(1)
    expect(calls.filter((call) => call.url === '/api/v1/scripts/')).toHaveLength(1)

    await findButton(wrapper, '重试').trigger('click')
    await flushPromises()

    expect(calls.filter((call) => call.url === '/api/v1/executions/stats')).toHaveLength(2)
    expect(calls.filter((call) => call.url === '/api/v1/scripts/')).toHaveLength(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('重试后接口')
    expect(wrapper.text()).toContain('9')
    expect(consoleError).toHaveBeenCalled()
  })

  it('opens recent script details using the returned script_id', async () => {
    respond = (call) =>
      call.url === '/api/v1/executions/stats'
        ? statsEnvelope()
        : scriptsEnvelope([script('akshare.stock_zh_a_hist', '历史行情', '行情')])

    const { wrapper, router } = await mountHome()
    await flushPromises()

    expect(wrapper.text()).toContain('历史行情')
    await findButton(wrapper, '查看').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/scripts/akshare.stock_zh_a_hist')
  })

  it('routes each quick action to its user-facing destination', async () => {
    const { wrapper, router } = await mountHome()
    await flushPromises()

    for (const [label, path] of [
      ['浏览数据接口', '/scripts'],
      ['管理定时任务', '/tasks'],
      ['查看数据表', '/tables'],
    ]) {
      await findButton(wrapper, label).trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.fullPath).toBe(path)
    }
  })
})
