/* eslint vue/one-component-per-file: off */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { mount, flushPromises, enableAutoUnmount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, Fragment, h, type PropType, type VNode } from 'vue'
import { ElMessage } from 'element-plus'
import request from '@/utils/request'
import ScriptsView from '@/views/ScriptsView.vue'

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface RequestCall {
  url: string
  method: string
  params: unknown
}

const calls: RequestCall[] = []
let respond: (call: RequestCall) => WireEnvelope | Promise<WireEnvelope> = (call) => {
  if (call.url === '/api/v1/scripts/categories') {
    return { success: true, data: ['行情', '宏观'] }
  }
  return scriptsEnvelope([])
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
    return () => h('div', { 'data-testid': 'script-destination' })
  },
})

function flattenNodes(nodes: VNode[]): VNode[] {
  return nodes.flatMap((node) => {
    if (node.type === Fragment && Array.isArray(node.children)) {
      return flattenNodes(node.children as VNode[])
    }
    return [node]
  })
}

const stubs = {
  ElCard: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => h('section', attrs, [slots.header?.(), slots.default?.()])
    },
  }),
  ElAlert: defineComponent({
    props: { title: { type: String, default: '' } },
    setup(props, { slots }) {
      return () => h('aside', { role: 'note' }, [h('span', props.title), slots.default?.()])
    },
  }),
  ElTag: defineComponent({
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  ElIcon: defineComponent({
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  Search: defineComponent({ render: () => h('span') }),
  ElInput: defineComponent({
    props: {
      modelValue: { type: String, default: '' },
      placeholder: { type: String, default: '' },
      clearable: { type: Boolean, default: false },
    },
    emits: ['update:modelValue', 'input'],
    setup(props, { emit, slots }) {
      function update(value: string) {
        emit('update:modelValue', value)
        emit('input', value)
      }
      return () =>
        h('div', [
          slots.prefix?.(),
          h('input', {
            'aria-label': '搜索接口名称或描述',
            placeholder: props.placeholder,
            value: props.modelValue,
            onInput: (event: Event) => update((event.target as HTMLInputElement).value),
          }),
          props.clearable && props.modelValue
            ? h(
                'button',
                { type: 'button', 'aria-label': '清除搜索', onClick: () => update('') },
                '清除'
              )
            : null,
        ])
    },
  }),
  ElRadioGroup: defineComponent({
    props: { modelValue: { type: String, default: '' } },
    emits: ['update:modelValue', 'change'],
    setup(props, { slots, emit }) {
      return () =>
        h(
          'div',
          { role: 'group', 'aria-label': '接口类别筛选' },
          flattenNodes(slots.default?.() ?? []).map((node) => {
            const value = String(node.props?.value ?? '')
            const children = node.children as string | { default?: () => VNode[] } | null
            const labelChildren = typeof children === 'object' ? (children?.default?.() ?? []) : []
            const label =
              typeof children === 'string'
                ? children.trim()
                : labelChildren.map((child) => String(child.children ?? '')).join('')
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
              label.trim()
            )
          })
        )
    },
  }),
  ElRadioButton: defineComponent({
    props: { value: { type: String, default: '' } },
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
  ElTable: defineComponent({
    props: {
      data: { type: Array as PropType<Record<string, unknown>[]>, default: () => [] },
    },
    setup(props, { slots }) {
      return () => {
        const columns = slots.default?.() ?? []
        return h(
          'table',
          { 'data-testid': 'scripts-table' },
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
        h('nav', { 'aria-label': '脚本列表分页' }, [
          h('span', `共 ${props.total} 个接口`),
          h(
            'button',
            {
              type: 'button',
              'aria-label': '上一页',
              onClick: () => {
                const page = Math.max(1, props.currentPage - 1)
                emit('update:currentPage', page)
                emit('current-change', page)
              },
            },
            '上一页'
          ),
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
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  calls.length = 0
  respond = (call) => {
    if (call.url === '/api/v1/scripts/categories') {
      return { success: true, data: ['行情', '宏观'] }
    }
    return scriptsEnvelope([])
  }
  request.defaults.adapter = adapter
  setActivePinia(createPinia())
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
})

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/scripts', component: ScriptsView },
      { path: '/scripts/:id', component: DestinationView },
    ],
  })
}

async function mountScripts() {
  const router = makeRouter()
  await router.push('/scripts')
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(ScriptsView, {
    global: {
      plugins: [pinia, router],
      stubs,
    },
  })
  return { wrapper, router }
}

function scriptsEnvelope(items: unknown[], total = items.length): WireEnvelope {
  return {
    success: true,
    data: { items, total, page: 1, page_size: 20 },
  }
}

function script(
  scriptId: string,
  name: string,
  category: string,
  description: string | null = null
) {
  return {
    id: 1,
    script_id: scriptId,
    script_name: name,
    category,
    sub_category: null,
    frequency: null,
    description,
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

function listCalls() {
  return calls.filter((call) => call.url === '/api/v1/scripts/')
}

function findButton(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
  expect(button, `button named ${label}`).toBeDefined()
  return button!
}

describe('ScriptsView operator flow', () => {
  it('loads categories and the first script page, shows loading, and renders returned rows', async () => {
    const listGate = deferred<WireEnvelope>()
    respond = (call) => {
      if (call.url === '/api/v1/scripts/categories') {
        return { success: true, data: ['行情', '宏观'] }
      }
      return listGate.promise
    }

    const { wrapper } = await mountScripts()
    await flushPromises()

    expect(calls).toHaveLength(2)
    expect(calls).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          url: '/api/v1/scripts/categories',
          method: 'get',
          params: undefined,
        }),
        expect.objectContaining({
          url: '/api/v1/scripts/',
          method: 'get',
          params: { page: 1, page_size: 20, category: undefined, keyword: undefined },
        }),
      ])
    )
    expect(wrapper.find('.el-loading-mask').exists()).toBe(true)
    expect(wrapper.get('[data-testid="scripts-table"] tbody').findAll('tr')).toHaveLength(0)

    listGate.resolve(
      scriptsEnvelope(
        [
          script('akshare.stock_daily', '日线行情', '行情', '获取每日收盘行情'),
          script('fred.gdp', '美国 GDP', '宏观', '国内生产总值'),
        ],
        2
      )
    )
    await flushPromises()

    expect(wrapper.text()).toContain('日线行情')
    expect(wrapper.text()).toContain('获取每日收盘行情')
    expect(wrapper.text()).toContain('美国 GDP')
    expect(wrapper.text()).toContain('共 2 个接口')
    expect(wrapper.get('[role="group"]').text()).toContain('宏观')
  })

  it('shows no script rows and a zero count when the backend returns an empty page', async () => {
    const { wrapper } = await mountScripts()
    await flushPromises()

    expect(listCalls()).toHaveLength(1)
    expect(wrapper.get('[data-testid="scripts-table"] tbody').findAll('tr')).toHaveLength(0)
    expect(wrapper.text()).toContain('共 0 个接口')
  })

  it('debounces search, applies category selection, and clears each filter in request params', async () => {
    vi.useFakeTimers()
    const { wrapper } = await mountScripts()
    await flushPromises()

    await wrapper.get('input[aria-label="搜索接口名称或描述"]').setValue('daily')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect(listCalls().at(-1)?.params).toEqual({
      page: 1,
      page_size: 20,
      category: undefined,
      keyword: 'daily',
    })

    await findButton(wrapper, '宏观').trigger('click')
    await flushPromises()
    expect(listCalls().at(-1)?.params).toEqual({
      page: 1,
      page_size: 20,
      category: '宏观',
      keyword: 'daily',
    })

    await findButton(wrapper, '清除').trigger('click')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect(listCalls().at(-1)?.params).toEqual({
      page: 1,
      page_size: 20,
      category: '宏观',
      keyword: undefined,
    })

    await findButton(wrapper, '全部').trigger('click')
    await flushPromises()
    expect(listCalls().at(-1)?.params).toEqual({
      page: 1,
      page_size: 20,
      category: undefined,
      keyword: undefined,
    })
  })

  it('requests the next page after the operator advances pagination', async () => {
    respond = () => scriptsEnvelope([script('market.daily', '日线行情', '行情')], 60)
    const { wrapper } = await mountScripts()
    await flushPromises()
    const initialListCalls = listCalls().length

    await wrapper.get('button[aria-label="下一页"]').trigger('click')
    await flushPromises()

    expect(listCalls()).toHaveLength(initialListCalls + 1)
    expect(listCalls().at(-1)?.params).toEqual({
      page: 2,
      page_size: 20,
      category: undefined,
      keyword: undefined,
    })
  })

  it('requests the selected page size and resets to page one', async () => {
    respond = () => scriptsEnvelope([script('market.daily', '日线行情', '行情')], 60)
    const { wrapper } = await mountScripts()
    await flushPromises()

    await wrapper.get('select[aria-label="每页条数"]').setValue('50')
    await flushPromises()

    expect(listCalls()).toHaveLength(2)
    expect(listCalls().at(-1)?.params).toEqual({
      page: 1,
      page_size: 50,
      category: undefined,
      keyword: undefined,
    })
  })

  it('keeps search and category parameters while advancing and returning a page', async () => {
    vi.useFakeTimers()
    const { wrapper } = await mountScripts()
    await flushPromises()

    await wrapper.get('input[aria-label="搜索接口名称或描述"]').setValue('daily')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    await findButton(wrapper, '宏观').trigger('click')
    await flushPromises()

    await findButton(wrapper, '下一页').trigger('click')
    await flushPromises()
    expect(listCalls().at(-1)?.params).toEqual({
      page: 2,
      page_size: 20,
      category: '宏观',
      keyword: 'daily',
    })

    await findButton(wrapper, '上一页').trigger('click')
    await flushPromises()
    expect(listCalls().at(-1)?.params).toEqual({
      page: 1,
      page_size: 20,
      category: '宏观',
      keyword: 'daily',
    })
    expect(listCalls()).toHaveLength(5)
  })

  it('navigates to the script details URL using script_id', async () => {
    respond = (call) =>
      call.url === '/api/v1/scripts/categories'
        ? { success: true, data: ['行情'] }
        : scriptsEnvelope([script('akshare.stock_zh_a_hist', '历史行情', '行情')])
    const { wrapper, router } = await mountScripts()
    await flushPromises()

    expect(wrapper.text()).toContain('历史行情')
    await findButton(wrapper, '查看详情').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/scripts/akshare.stock_zh_a_hist')
  })

  it('reports a list request failure and stops the loading state', async () => {
    const messageError = vi.spyOn(ElMessage, 'error').mockImplementation(() => undefined as never)
    respond = (call) =>
      call.url === '/api/v1/scripts/categories'
        ? { success: true, data: ['行情'] }
        : { success: false, message: '接口目录暂不可用' }
    const { wrapper } = await mountScripts()
    await flushPromises()

    expect(messageError).toHaveBeenCalledWith('接口目录暂不可用')
    expect(wrapper.get('[data-testid="scripts-table"] tbody').findAll('tr')).toHaveLength(0)
  })
})
