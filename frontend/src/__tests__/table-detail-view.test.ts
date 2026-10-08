/* eslint vue/one-component-per-file: off */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { mount, flushPromises, enableAutoUnmount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { defineComponent, Fragment, h, type PropType, type VNode } from 'vue'
import { ElMessage } from 'element-plus'
import request from '@/utils/request'
import TableDetailView from '@/views/TableDetailView.vue'

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface RequestCall {
  url: string
  method: string
  params: unknown
  signal: InternalAxiosRequestConfig['signal']
}

const calls: RequestCall[] = []
let respond: (call: RequestCall) => WireEnvelope | Promise<WireEnvelope> = () => ({
  success: true,
  data: { table_name: 'unknown', columns: [], row_count: 0, last_update_time: null },
})
const originalAdapter = request.defaults.adapter

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig
): Promise<AxiosResponse> => {
  const call = {
    url: `${config.baseURL ?? ''}${config.url ?? ''}`,
    method: (config.method ?? 'get').toLowerCase(),
    params: config.params,
    signal: config.signal,
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

function schema(
  tableId: string,
  columns = [
    {
      name: 'symbol',
      type: 'varchar(16)',
      nullable: false,
      key: 'PRI',
      default: null,
    },
    {
      name: 'trade_date',
      type: 'date',
      nullable: true,
      key: 'MUL',
      default: null,
    },
  ]
) {
  return {
    table_name: `market_${tableId}`,
    columns,
    row_count: 3,
    last_update_time: null,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function columnSlots(node: VNode, row: Record<string, unknown>, index: number) {
  const children = node.children as {
    default?: (scope: { row: unknown; $index: number }) => VNode[]
  } | null
  return children?.default?.({ row, $index: index })
}

function tableColumns(nodes: VNode[]): VNode[] {
  return nodes.flatMap((node) => {
    if (node.type === Fragment && Array.isArray(node.children)) {
      return tableColumns(node.children as VNode[])
    }
    return node.props && ('prop' in node.props || 'label' in node.props) ? [node] : []
  })
}

const stubs = {
  ElPageHeader: defineComponent({
    setup(_props, { slots, emit }) {
      return () =>
        h('header', [
          h('button', { type: 'button', onClick: () => emit('back') }, '返回'),
          slots.content?.(),
        ])
    },
  }),
  ElCard: defineComponent({
    setup(_props, { slots }) {
      return () => h('section', [slots.header?.(), slots.default?.()])
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
              const label = String(pane.props?.label ?? name)
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
                label
              )
            })
          ),
          active ? (active.children as { default?: () => VNode[] }).default?.() : null,
        ])
      }
    },
  }),
  ElTable: defineComponent({
    props: { data: { type: Array as PropType<Record<string, unknown>[]>, default: () => [] } },
    setup(props, { slots }) {
      return () => {
        const columns = tableColumns(slots.default?.() ?? [])
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
  ElTag: defineComponent({
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  ElEmpty: defineComponent({
    props: { description: { type: String, default: '' } },
    setup(props) {
      return () => h('div', { 'data-testid': 'empty' }, props.description)
    },
  }),
}

const OutsideView = defineComponent({
  setup() {
    return () => h('div', 'outside detail')
  },
})

enableAutoUnmount(afterEach)

beforeEach(() => {
  calls.length = 0
  respond = () => ({ success: true, data: schema('0') })
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
      { path: '/tables/:id', component: TableDetailView },
      { path: '/outside', component: OutsideView },
    ],
  })
}

async function mountDetailAt(tableId: string) {
  const router = makeRouter()
  await router.push(`/tables/${tableId}`)
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(RouterView, {
    global: {
      plugins: [pinia, router],
      stubs,
      directives: { loading: {} },
    },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('TableDetailView operator flow', () => {
  it('loads schema by route identity and displays nullable and key metadata', async () => {
    respond = (call) => ({
      success: true,
      message: 'success',
      data: schema(call.url.split('/')[4]),
    })

    const { wrapper } = await mountDetailAt('802')

    expect(calls).toContainEqual(
      expect.objectContaining({ url: '/api/v1/tables/802/schema', method: 'get' })
    )
    expect(wrapper.text()).toContain('market_802')
    const rows = wrapper.findAll('tbody tr').map((row) => row.text())
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain('symbol')
    expect(rows[0]).toContain('否')
    expect(rows[0]).toContain('PRI')
    expect(rows[1]).toContain('trade_date')
    expect(rows[1]).toContain('是')
    expect(rows[1]).toContain('MUL')
  })

  it('requests preview page 1 with page_size 100 and reuses the loaded preview on tab revisit', async () => {
    respond = (call) => {
      if (call.url.endsWith('/data')) {
        return {
          success: true,
          message: 'success',
          data: {
            table_name: 'market_803',
            columns: ['symbol', 'close'],
            rows: [{ symbol: '600000', close: 8.2 }],
            row_count: 1,
          },
        }
      }
      return { success: true, data: schema('803') }
    }

    const { wrapper } = await mountDetailAt('803')

    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/803/data')).toEqual([
      expect.objectContaining({
        method: 'get',
        params: { page: 1, page_size: 100 },
      }),
    ])
    expect(wrapper.text()).toContain('600000')

    await wrapper.get('[data-tab="schema"]').trigger('click')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/803/data')).toHaveLength(1)
  })

  it('shows a preview failure notice and retries when the operator reopens preview', async () => {
    let previewAttempts = 0
    respond = (call) => {
      if (call.url.endsWith('/data')) {
        previewAttempts += 1
        if (previewAttempts === 1) return { success: false, message: '预览暂不可用' }
        return {
          success: true,
          data: {
            table_name: 'market_804',
            columns: ['symbol'],
            rows: [{ symbol: 'retry_ok' }],
            row_count: 1,
          },
        }
      }
      return { success: true, data: schema('804') }
    }
    const messageError = vi.spyOn(ElMessage, 'error')

    const { wrapper } = await mountDetailAt('804')

    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(messageError).toHaveBeenCalledWith('预览暂不可用')
    expect(wrapper.text()).toContain('暂无数据')

    await wrapper.get('[data-tab="schema"]').trigger('click')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()

    expect(calls.filter((call) => call.url === '/api/v1/tables/804/data')).toHaveLength(2)
    expect(wrapper.text()).toContain('retry_ok')
  })

  it('reloads schema for a new route id and ignores an older schema response and finally', async () => {
    const oldSchema = deferred<WireEnvelope>()
    const newSchema = deferred<WireEnvelope>()
    respond = (call) => {
      if (call.url === '/api/v1/tables/805/schema') return oldSchema.promise
      if (call.url === '/api/v1/tables/806/schema') return newSchema.promise
      return { success: true, data: schema('unexpected') }
    }

    const { wrapper, router } = await mountDetailAt('805')
    const oldSchemaCall = calls.find((call) => call.url === '/api/v1/tables/805/schema')
    expect(oldSchemaCall).toBeDefined()
    expect(oldSchemaCall?.signal?.aborted).toBe(false)

    await router.push('/tables/806')
    await flushPromises()
    expect(oldSchemaCall?.signal?.aborted).toBe(true)
    expect(calls).toContainEqual(expect.objectContaining({ url: '/api/v1/tables/806/schema' }))
    expect(wrapper.text()).not.toContain('market_805')
    expect(
      wrapper
        .findAll('.el-loading-mask')
        .some((mask) => !mask.attributes('style')?.includes('display: none'))
    ).toBe(true)

    oldSchema.resolve({ success: true, data: schema('805') })
    await flushPromises()
    expect(wrapper.text()).not.toContain('market_805')
    expect(
      wrapper
        .findAll('.el-loading-mask')
        .some((mask) => !mask.attributes('style')?.includes('display: none'))
    ).toBe(true)

    newSchema.resolve({ success: true, data: schema('806') })
    await flushPromises()
    expect(wrapper.text()).toContain('market_806')
    expect(wrapper.text()).not.toContain('market_805')
  })

  it('isolates preview cache by route id and ignores an older preview that completes late', async () => {
    const oldPreview = deferred<WireEnvelope>()
    const newPreview = deferred<WireEnvelope>()
    respond = (call) => {
      if (call.url.endsWith('/schema')) {
        const tableId = call.url.split('/')[4]
        return { success: true, data: schema(tableId) }
      }
      if (call.url === '/api/v1/tables/805/data') return oldPreview.promise
      if (call.url === '/api/v1/tables/806/data') return newPreview.promise
      return { success: true, data: schema('unexpected') }
    }

    const { wrapper, router } = await mountDetailAt('805')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    const oldPreviewCall = calls.find((call) => call.url === '/api/v1/tables/805/data')
    expect(oldPreviewCall).toEqual(expect.objectContaining({ params: { page: 1, page_size: 100 } }))

    await router.push('/tables/806')
    await flushPromises()
    expect(oldPreviewCall?.signal?.aborted).toBe(true)
    expect(wrapper.text()).toContain('market_806')
    expect(wrapper.text()).not.toContain('market_805')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/806/data')).toHaveLength(1)
    expect(
      wrapper
        .findAll('.el-loading-mask')
        .some((mask) => !mask.attributes('style')?.includes('display: none'))
    ).toBe(true)

    oldPreview.resolve({
      success: true,
      data: {
        table_name: 'market_805',
        columns: ['symbol'],
        rows: [{ symbol: 'stale_route_row' }],
        row_count: 1,
      },
    })
    await flushPromises()
    expect(wrapper.text()).not.toContain('stale_route_row')
    expect(
      wrapper
        .findAll('.el-loading-mask')
        .some((mask) => !mask.attributes('style')?.includes('display: none'))
    ).toBe(true)

    newPreview.resolve({
      success: true,
      data: {
        table_name: 'market_806',
        columns: ['symbol'],
        rows: [{ symbol: 'new_route_row' }],
        row_count: 1,
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('new_route_row')

    await wrapper.get('[data-tab="schema"]').trigger('click')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/806/data')).toHaveLength(1)
  })

  it('loads and caches preview independently for the newly selected table id', async () => {
    respond = (call) => {
      if (call.url.endsWith('/schema')) {
        const tableId = call.url.split('/')[4]
        return { success: true, data: schema(tableId) }
      }
      const tableId = call.url.split('/')[4]
      return {
        success: true,
        data: {
          table_name: `market_${tableId}`,
          columns: ['symbol'],
          rows: [{ symbol: `row_for_${tableId}` }],
          row_count: 1,
        },
      }
    }

    const { wrapper, router } = await mountDetailAt('807')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('row_for_807')

    await router.push('/tables/808')
    await flushPromises()
    expect(wrapper.text()).not.toContain('row_for_807')
    expect(wrapper.text()).toContain('market_808')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()

    expect(calls.filter((call) => call.url === '/api/v1/tables/807/data')).toHaveLength(1)
    expect(calls.filter((call) => call.url === '/api/v1/tables/808/data')).toEqual([
      expect.objectContaining({ params: { page: 1, page_size: 100 } }),
    ])
    expect(wrapper.text()).toContain('row_for_808')

    await wrapper.get('[data-tab="schema"]').trigger('click')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(calls.filter((call) => call.url === '/api/v1/tables/808/data')).toHaveLength(1)
  })

  it('aborts its in-flight preview when leaving the detail route', async () => {
    const pendingPreview = deferred<WireEnvelope>()
    respond = (call) => {
      if (call.url.endsWith('/schema')) return { success: true, data: schema('809') }
      return pendingPreview.promise
    }
    const messageError = vi.spyOn(ElMessage, 'error')

    const { wrapper, router } = await mountDetailAt('809')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    const previewCall = calls.find((call) => call.url === '/api/v1/tables/809/data')
    expect(previewCall?.signal?.aborted).toBe(false)

    await router.push('/outside')
    await flushPromises()
    expect(previewCall?.signal?.aborted).toBe(true)

    pendingPreview.resolve({ success: false, message: 'unmounted preview failure' })
    await flushPromises()
    expect(messageError).not.toHaveBeenCalledWith('unmounted preview failure')
    expect(router.currentRoute.value.fullPath).toBe('/outside')
    expect(wrapper.text()).toContain('outside detail')
  })

  it('does not show an old route preview failure after switching to another table', async () => {
    const oldPreview = deferred<WireEnvelope>()
    respond = (call) => {
      if (call.url.endsWith('/schema')) {
        const tableId = call.url.split('/')[4]
        return { success: true, data: schema(tableId) }
      }
      if (call.url === '/api/v1/tables/805/data') return oldPreview.promise
      return {
        success: true,
        data: {
          table_name: 'market_806',
          columns: ['symbol'],
          rows: [{ symbol: 'current_route_row' }],
          row_count: 1,
        },
      }
    }
    const messageError = vi.spyOn(ElMessage, 'error')

    const { wrapper, router } = await mountDetailAt('805')
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await router.push('/tables/806')
    await flushPromises()
    await wrapper.get('[data-tab="preview"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('current_route_row')

    oldPreview.resolve({ success: false, message: '旧表预览失败' })
    await flushPromises()

    expect(messageError).not.toHaveBeenCalledWith('旧表预览失败')
    expect(wrapper.text()).toContain('current_route_row')
    expect(wrapper.text()).not.toContain('market_805')
  })
})
