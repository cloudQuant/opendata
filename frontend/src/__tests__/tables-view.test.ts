/* eslint vue/one-component-per-file: off */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { mount, flushPromises, enableAutoUnmount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { defineComponent, h, type PropType, type VNode } from 'vue'
import request from '@/utils/request'
import TablesView from '@/views/TablesView.vue'
import TableDetailView from '@/views/TableDetailView.vue'

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface RequestCall {
  url: string
  method: string
  params: unknown
}

const calls: RequestCall[] = []
let respond: (call: RequestCall) => WireEnvelope = () => ({
  success: true,
  data: { items: [], total: 0, page: 1, page_size: 20 },
})
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
    data: respond(call),
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/tables', component: TablesView },
      { path: '/tables/:id', component: TableDetailView },
    ],
  })
}

function table(id: number, tableName = `market_${id}`) {
  return {
    id,
    table_name: tableName,
    table_comment: null,
    category: 'market',
    script_id: null,
    row_count: 12,
    last_update_time: null,
    last_update_status: null,
    created_at: '2026-10-08T00:00:00Z',
    updated_at: '2026-10-08T00:00:00Z',
  }
}

function tableList(items: unknown[], total = items.length): WireEnvelope {
  return { success: true, message: 'success', data: { items, total, page: 1, page_size: 20 } }
}

function columnSlots(node: VNode, row: Record<string, unknown>, index: number) {
  const children = node.children as {
    default?: (scope: { row: unknown; $index: number }) => VNode[]
  } | null
  return children?.default?.({ row, $index: index })
}

const stubs = {
  ElCard: defineComponent({
    setup(_props, { slots }) {
      return () => h('section', [slots.header?.(), slots.default?.()])
    },
  }),
  ElTable: defineComponent({
    props: { data: { type: Array as PropType<Record<string, unknown>[]>, default: () => [] } },
    setup(props, { slots }) {
      return () => {
        const columns = (slots.default?.() ?? []).filter((node) => node.props !== null)
        return h(
          'table',
          { 'data-testid': 'el-table' },
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
  ElButton: defineComponent({
    inheritAttrs: false,
    setup(_props, { attrs, slots, emit }) {
      return () =>
        h(
          'button',
          { ...attrs, type: 'button', onClick: (event: Event) => emit('click', event) },
          slots.default?.()
        )
    },
  }),
  ElInput: defineComponent({
    props: { modelValue: { type: String, default: '' } },
    emits: ['update:modelValue', 'input'],
    setup(props, { emit }) {
      return () =>
        h('input', {
          'aria-label': '搜索表名',
          value: props.modelValue,
          onInput: (event: Event) => {
            const value = (event.target as HTMLInputElement).value
            emit('update:modelValue', value)
            emit('input', value)
          },
        })
    },
  }),
  ElPagination: defineComponent({
    props: {
      currentPage: { type: Number, default: 1 },
      pageSize: { type: Number, default: 20 },
      pageSizes: { type: Array as PropType<number[]>, default: () => [] },
      total: { type: Number, default: 0 },
    },
    emits: ['update:currentPage', 'update:pageSize', 'current-change', 'size-change'],
    setup(props, { emit }) {
      return () =>
        h('nav', { 'aria-label': '分页' }, [
          h('span', `共 ${props.total} 条`),
          h(
            'button',
            {
              type: 'button',
              'aria-label': '下一页',
              onClick: () => {
                const page = props.currentPage + 1
                emit('update:currentPage', page)
                emit('current-change', page)
              },
            },
            '下一页'
          ),
          h(
            'select',
            {
              'aria-label': '每页条数',
              value: props.pageSize,
              onChange: (event: Event) => {
                const size = Number((event.target as HTMLSelectElement).value)
                emit('update:pageSize', size)
                emit('size-change', size)
              },
            },
            props.pageSizes.map((size) => h('option', { value: size }, String(size)))
          ),
        ])
    },
  }),
  ElRadioGroup: defineComponent({
    props: { modelValue: { type: String, default: 'all' } },
    emits: ['update:modelValue', 'change'],
    setup(props, { slots, emit }) {
      return () =>
        h(
          'div',
          { role: 'group', 'aria-label': '数仓层' },
          (slots.default?.() ?? []).map((node) => {
            const value = String(node.props?.value ?? '')
            const label = (node.children as { default?: () => VNode[] } | null)?.default?.() ?? []
            return h(
              'button',
              {
                type: 'button',
                'aria-pressed': String(props.modelValue === value),
                onClick: () => {
                  emit('update:modelValue', value)
                  emit('change', value)
                },
              },
              label
            )
          })
        )
    },
  }),
  ElTag: defineComponent({
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  ElAlert: defineComponent({
    props: { title: { type: String, default: '' } },
    setup(props, { slots }) {
      return () => h('aside', { role: 'alert' }, [h('span', props.title), slots.default?.()])
    },
  }),
  ElIcon: defineComponent({
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  Search: defineComponent({ render: () => h('span') }),
  ElPageHeader: defineComponent({
    setup(_props, { slots }) {
      return () => h('header', slots.content?.())
    },
  }),
  ElTabs: defineComponent({
    props: { modelValue: { type: String, default: 'schema' } },
    emits: ['update:modelValue', 'tab-change'],
    setup(props, { slots, emit }) {
      return () => {
        const panes = slots.default?.() ?? []
        const active = panes.find((pane) => pane.props?.name === props.modelValue)
        return h('div', [
          h(
            'nav',
            { 'aria-label': '表详情标签' },
            panes.map((pane) => {
              const name = String(pane.props?.name ?? '')
              return h(
                'button',
                {
                  type: 'button',
                  'data-tab': name,
                  onClick: () => {
                    emit('update:modelValue', name)
                    emit('tab-change', name)
                  },
                },
                String(pane.props?.label ?? name)
              )
            })
          ),
          active ? (active.children as { default?: () => VNode[] }).default?.() : null,
        ])
      }
    },
  }),
  ElEmpty: defineComponent({
    props: { description: { type: String, default: '' } },
    setup(props) {
      return () => h('div', { 'data-testid': 'empty' }, props.description)
    },
  }),
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  calls.length = 0
  respond = () => tableList([])
  request.defaults.adapter = adapter
  setActivePinia(createPinia())
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
  vi.useRealTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

async function mountTablesView() {
  const router = makeRouter()
  await router.push('/tables')
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(TablesView, {
    global: {
      plugins: [pinia, router],
      stubs,
      directives: { loading: {} },
    },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('TablesView operator flow', () => {
  it('loads the first page and displays the returned table and warehouse layer', async () => {
    respond = (call) => {
      if (call.url.endsWith('/tables/warehouse')) {
        return {
          success: true,
          data: {
            count: 1,
            tables: [
              {
                table: 'ods_market',
                layer: 'ods',
                domain: 'market',
                source: 'vendor',
                rows: 12,
                size_mb: 1,
              },
            ],
          },
        }
      }
      return tableList([table(17, 'market_daily')], 1)
    }

    const { wrapper } = await mountTablesView()

    expect(calls).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          url: '/api/v1/tables/',
          method: 'get',
          params: { page: 1, page_size: 20, search: undefined },
        }),
        expect.objectContaining({
          url: '/api/v1/tables/warehouse',
          method: 'get',
          params: { layer: 'all' },
        }),
      ])
    )
    expect(wrapper.text()).toContain('market_daily')
    expect(wrapper.text()).toContain('ods_market')
    expect(wrapper.text()).toContain('共 1 个表')
  })

  it('renders the empty-list state from a successful empty API envelope', async () => {
    const { wrapper } = await mountTablesView()

    expect(calls.some((call) => call.url === '/api/v1/tables/')).toBe(true)
    expect(wrapper.text()).toContain('共 0 个表')
    expect(wrapper.text()).not.toContain('market_')
  })

  it('shows a list error and retries the same API request from the alert', async () => {
    let listAttempts = 0
    respond = (call) => {
      if (call.url === '/api/v1/tables/') {
        listAttempts += 1
        return listAttempts === 1
          ? { success: false, message: '临时读取失败' }
          : tableList([table(18, 'after_retry')], 1)
      }
      return { success: true, data: { count: 0, tables: [] } }
    }

    const { wrapper } = await mountTablesView()

    expect(wrapper.get('[role="alert"]').text()).toContain('临时读取失败')
    await wrapper.get('[role="alert"] button').trigger('click')
    await flushPromises()

    expect(calls.filter((call) => call.url === '/api/v1/tables/')).toHaveLength(2)
    expect(wrapper.text()).toContain('after_retry')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('sends debounced search, page, and page-size changes as backend pagination parameters', async () => {
    vi.useFakeTimers()
    const { wrapper } = await mountTablesView()

    await wrapper.get('input[aria-label="搜索表名"]').setValue('daily')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/').at(-1)?.params).toEqual({
      page: 1,
      page_size: 20,
      search: 'daily',
    })

    await wrapper.get('button[aria-label="下一页"]').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/').at(-1)?.params).toEqual({
      page: 2,
      page_size: 20,
      search: 'daily',
    })

    await wrapper.get('select[aria-label="每页条数"]').setValue('50')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/').at(-1)?.params).toEqual({
      page: 1,
      page_size: 50,
      search: 'daily',
    })
  })

  it('requests ods and dwd layers and navigates by the selected numeric table id', async () => {
    const schema = {
      table_name: 'market_19',
      columns: [{ name: 'symbol', type: 'varchar', nullable: false, key: 'PRI', default: null }],
      row_count: 12,
      last_update_time: null,
    }
    respond = (call) => {
      if (call.url === '/api/v1/tables/warehouse') {
        const layer = (call.params as { layer: string }).layer
        return {
          success: true,
          data: {
            count: 1,
            tables: [
              {
                table: `${layer}_market`,
                layer: layer === 'all' ? 'ods' : layer,
                domain: 'market',
                source: null,
                rows: 1,
                size_mb: 1,
              },
            ],
          },
        }
      }
      if (call.url === '/api/v1/tables/19/schema') return { success: true, data: schema }
      return tableList([table(19, 'market_19')], 1)
    }

    const router = makeRouter()
    await router.push('/tables')
    await router.isReady()
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(RouterView, {
      global: { plugins: [pinia, router], stubs, directives: { loading: {} } },
    })
    await flushPromises()

    await wrapper.get('button[aria-pressed="false"]:nth-child(2)').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/warehouse').at(-1)?.params).toEqual({
      layer: 'ods',
    })
    await wrapper.get('button[aria-pressed="false"]:nth-child(3)').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/warehouse').at(-1)?.params).toEqual({
      layer: 'dwd',
    })

    const detailButton = wrapper.findAll('button').find((button) => button.text() === '查看详情')
    expect(detailButton).toBeDefined()
    await detailButton!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/tables/19')
    expect(calls).toContainEqual(
      expect.objectContaining({ url: '/api/v1/tables/19/schema', method: 'get' })
    )
    expect(wrapper.text()).toContain('market_19')
  })
})
