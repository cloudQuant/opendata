/* eslint vue/one-component-per-file: off */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, type PropType } from 'vue'
import request from '@/utils/request'
import { useAuthStore } from '@/stores/auth'
import SettingsView from '@/views/SettingsView.vue'
import type { AuthUser } from '@/types'

const mocks = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  confirm: vi.fn(),
  prompt: vi.fn(),
}))

vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>('element-plus')
  return {
    ...actual,
    ElMessage: {
      success: mocks.success,
      error: mocks.error,
      warning: mocks.warning,
    },
    ElMessageBox: {
      ...actual.ElMessageBox,
      confirm: mocks.confirm,
      prompt: mocks.prompt,
    },
  }
})

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface RequestCall {
  url: string
  method: string
  params: unknown
  body: unknown
}

interface DatabaseConfigFixture {
  host: string
  port: number
  database: string
  user: string
  password: string
}

const MAIN_CONFIG: DatabaseConfigFixture = {
  host: 'main-db.internal',
  port: 3306,
  database: 'main_db',
  user: 'main_user',
  password: 'C32_MAIN_CONFIG_SECRET',
}

const WAREHOUSE_CONFIG: DatabaseConfigFixture = {
  host: 'warehouse-db.internal',
  port: 3307,
  database: 'warehouse_db',
  user: 'warehouse_user',
  password: 'C32_WAREHOUSE_CONFIG_SECRET',
}

const HEALTH = {
  status: 'healthy',
  version: 'C32_SETTINGS_FIXTURE_VERSION',
  database: 'connected',
  scheduler: 'running',
}

const requestCalls: RequestCall[] = []
let answer: (call: RequestCall) => WireEnvelope | Promise<WireEnvelope>
let validationResults: Record<string, boolean> = {}
const fetchHealth = vi.fn()
const originalAdapter = request.defaults.adapter

function readBody(data: unknown): unknown {
  if (typeof data !== 'string') return data
  try {
    return JSON.parse(data) as unknown
  } catch {
    return data
  }
}

const adapter: AxiosAdapter = async (
  config: InternalAxiosRequestConfig
): Promise<AxiosResponse> => {
  const call: RequestCall = {
    url: `${config.baseURL ?? ''}${config.url ?? ''}`,
    method: (config.method ?? 'get').toLowerCase(),
    params: config.params,
    body: readBody(config.data),
  }
  requestCalls.push(call)
  return {
    data: await answer(call),
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
}

function success(data: unknown): WireEnvelope {
  return { success: true, message: 'success', data }
}

function defaultAnswer(call: RequestCall): WireEnvelope {
  if (call.method === 'get' && call.url === '/api/v1/settings/database') return success(MAIN_CONFIG)
  if (call.method === 'get' && call.url === '/api/v1/settings/database/warehouse') {
    return success(WAREHOUSE_CONFIG)
  }
  if (call.method === 'post' && call.url === '/api/v1/settings/database/test') {
    return success({ success: true, message: 'C32_MAIN_CONNECTION_OK' })
  }
  if (call.method === 'post' && call.url === '/api/v1/settings/database/warehouse/test') {
    return success({ success: true, message: 'C32_WAREHOUSE_CONNECTION_OK' })
  }
  return { success: false, message: `C32_UNEXPECTED_REQUEST_${call.method}_${call.url}` }
}

function databaseUser(role: AuthUser['role'] = 'admin'): AuthUser {
  return {
    user_id: 1,
    email: 'C32_ADMIN@example.test',
    role,
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: null,
  }
}

const DestinationView = defineComponent({
  setup() {
    return () => h('div', { 'data-testid': 'destination-view' })
  },
})

function testRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: DestinationView },
      { path: '/settings', component: DestinationView },
    ],
  })
}

const validation = (model: Record<string, unknown>): boolean =>
  validationResults[String(model.database ?? '')] ?? true

const stubs = {
  ElCard: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => {
        const header = slots.header?.()
        const title = slots.title?.()
        const body = slots.default?.()
        const footer = slots.footer?.()
        return h('section', attrs, [
          header?.length ? h('header', header) : null,
          title?.length ? h('div', { class: 'card-title' }, title) : null,
          h('div', { class: 'card-body' }, body),
          footer?.length ? h('footer', footer) : null,
        ])
      }
    },
  }),
  ElDescriptions: defineComponent({
    setup(_props, { slots }) {
      return () => h('div', { class: 'descriptions' }, slots.default?.())
    },
  }),
  ElDescriptionsItem: defineComponent({
    props: { label: { type: String, default: '' } },
    setup(props, { slots }) {
      return () => h('div', [h('span', props.label), slots.default?.()])
    },
  }),
  ElTag: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => h('span', attrs, slots.default?.())
    },
  }),
  ElRow: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => h('div', attrs, slots.default?.())
    },
  }),
  ElCol: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => h('div', attrs, slots.default?.())
    },
  }),
  ElForm: defineComponent({
    props: { model: { type: Object as PropType<Record<string, unknown>>, default: () => ({}) } },
    setup(props, { attrs, expose, slots }) {
      const clearValidate = vi.fn()
      const validateForm = vi.fn(async () => validation(props.model))
      expose({ clearValidate, validate: validateForm })
      return () =>
        h(
          'form',
          {
            ...attrs,
            'data-form-db': String(props.model.database ?? ''),
            onSubmit: (event: Event) => event.preventDefault(),
          },
          slots.default?.()
        )
    },
  }),
  ElFormItem: defineComponent({
    props: {
      label: { type: String, default: '' },
      prop: { type: String, default: '' },
    },
    setup(props, { slots }) {
      return () =>
        h('label', { 'data-prop': props.prop }, [
          h('span', props.label),
          h('span', { class: 'form-item-control' }, slots.default?.()),
        ])
    },
  }),
  ElInput: defineComponent({
    inheritAttrs: false,
    props: {
      modelValue: { type: [String, Number] as PropType<string | number>, default: '' },
      type: { type: String, default: 'text' },
      disabled: { type: Boolean, default: false },
    },
    emits: ['update:modelValue'],
    setup(props, { attrs, emit }) {
      return () =>
        h('input', {
          ...attrs,
          type: props.type,
          value: props.modelValue,
          disabled: props.disabled,
          onInput: (event: Event) =>
            emit('update:modelValue', (event.target as HTMLInputElement).value),
        })
    },
  }),
  ElInputNumber: defineComponent({
    inheritAttrs: false,
    props: {
      modelValue: { type: Number, default: 0 },
      disabled: { type: Boolean, default: false },
    },
    emits: ['update:modelValue'],
    setup(props, { attrs, emit }) {
      return () =>
        h('input', {
          ...attrs,
          type: 'number',
          value: props.modelValue,
          disabled: props.disabled,
          onInput: (event: Event) =>
            emit('update:modelValue', Number((event.target as HTMLInputElement).value)),
        })
    },
  }),
  ElButton: defineComponent({
    inheritAttrs: false,
    props: { loading: { type: Boolean, default: false } },
    emits: ['click'],
    setup(props, { attrs, emit, slots }) {
      return () =>
        h(
          'button',
          {
            ...attrs,
            type: 'button',
            disabled: props.loading || attrs.disabled,
            onClick: (event: Event) => emit('click', event),
          },
          slots.default?.()
        )
    },
  }),
  ElEmpty: defineComponent({
    props: { description: { type: String, default: '' } },
    setup(props) {
      return () => h('div', { 'data-testid': 'no-permission' }, props.description)
    },
  }),
  ElDialog: defineComponent({
    props: {
      modelValue: { type: Boolean, default: false },
      title: { type: String, default: '' },
    },
    emits: ['update:modelValue'],
    setup(props, { slots }) {
      return () =>
        props.modelValue
          ? h('section', { role: 'dialog', 'aria-label': props.title }, [
              h('h2', props.title),
              slots.default?.(),
              h('footer', slots.footer?.()),
            ])
          : null
    },
  }),
  ElAlert: defineComponent({
    props: { title: { type: String, default: '' }, description: { type: String, default: '' } },
    setup(props) {
      return () => h('aside', { role: 'alert' }, [props.title, props.description])
    },
  }),
}

function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ').trim()
}

function findButton(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
  expect(button, `button named ${label}`).toBeDefined()
  return button!
}

function findCardButton(wrapper: ReturnType<typeof mount>, cardIndex: number, label: string) {
  const card = wrapper.findAll('.config-card')[cardIndex]
  expect(card, `configuration card ${cardIndex}`).toBeDefined()
  const button = card.findAll('button').find((candidate) => candidate.text().trim() === label)
  expect(button, `button named ${label} in configuration card ${cardIndex}`).toBeDefined()
  return button!
}

function field(wrapper: ReturnType<typeof mount>, database: string, type: string) {
  return wrapper.find(`form[data-form-db="${database}"] input[type="${type}"]`)
}

async function mountSettings(role: AuthUser['role'] = 'admin') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = databaseUser(role)
  auth.accessToken = 'C32_SETTINGS_ACCESS_TOKEN'

  const router = testRouter()
  await router.push('/settings')
  await router.isReady()
  const wrapper = mount(SettingsView, { global: { plugins: [pinia, router], stubs } })
  await flushPromises()
  return { wrapper, router }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  requestCalls.length = 0
  answer = defaultAnswer
  validationResults = { main_db: true, warehouse_db: true }
  request.defaults.adapter = adapter
  mocks.success.mockReset()
  mocks.error.mockReset()
  mocks.warning.mockReset()
  mocks.confirm.mockReset()
  mocks.prompt.mockReset()
  fetchHealth.mockReset().mockResolvedValue({ json: async () => HEALTH })
  vi.stubGlobal('fetch', fetchHealth)
})

afterEach(() => {
  request.defaults.adapter = originalAdapter
  vi.unstubAllGlobals()
})

describe('SettingsView administrator flow', () => {
  it('redirects a non-admin without requesting configuration or health', async () => {
    const { wrapper, router } = await mountSettings('user')

    expect(router.currentRoute.value.fullPath).toBe('/')
    expect(flat(wrapper)).toContain('您没有权限访问此页面')
    expect(requestCalls).toEqual([])
    expect(fetchHealth).not.toHaveBeenCalled()
    expect(mocks.error).toHaveBeenCalledWith('权限不足')
  })

  it('starts health and both configuration reads together, then hides stored passwords', async () => {
    const mainGate = deferred<WireEnvelope>()
    const warehouseGate = deferred<WireEnvelope>()
    const healthGate = deferred<{ json: () => Promise<typeof HEALTH> }>()
    answer = (call) => {
      if (call.url === '/api/v1/settings/database') return mainGate.promise
      if (call.url === '/api/v1/settings/database/warehouse') return warehouseGate.promise
      return defaultAnswer(call)
    }
    fetchHealth.mockReturnValue(healthGate.promise)

    const { wrapper } = await mountSettings()

    expect(fetchHealth).toHaveBeenCalledWith('/health')
    expect(requestCalls.map(({ method, url }) => `${method} ${url}`).sort()).toEqual([
      'get /api/v1/settings/database',
      'get /api/v1/settings/database/warehouse',
    ])

    mainGate.resolve(success(MAIN_CONFIG))
    warehouseGate.resolve(success(WAREHOUSE_CONFIG))
    healthGate.resolve({ json: async () => HEALTH })
    await flushPromises()

    expect(flat(wrapper)).toContain('C32_SETTINGS_FIXTURE_VERSION')
    expect(flat(wrapper)).toContain('healthy')
    expect(flat(wrapper)).toContain('已连接')
    expect(field(wrapper, 'main_db', 'password').element.getAttribute('value')).toBe('')
    expect(field(wrapper, 'warehouse_db', 'password').element.getAttribute('value')).toBe('')
    expect(flat(wrapper)).not.toContain(MAIN_CONFIG.password)
    expect(flat(wrapper)).not.toContain(WAREHOUSE_CONFIG.password)
  })

  it('restores the loaded main database values when the user cancels editing', async () => {
    const { wrapper } = await mountSettings()
    await findCardButton(wrapper, 0, '编辑').trigger('click')

    const host = field(wrapper, 'main_db', 'text')
    await host.setValue('C32_EDITED_HOST')
    expect((host.element as HTMLInputElement).value).toBe('C32_EDITED_HOST')

    await findCardButton(wrapper, 0, '取消').trigger('click')
    await flushPromises()

    expect((field(wrapper, 'main_db', 'text').element as HTMLInputElement).value).toBe(
      MAIN_CONFIG.host
    )
    expect(findCardButton(wrapper, 0, '编辑').exists()).toBe(true)
    expect(requestCalls.some((call) => call.method === 'put')).toBe(false)
  })

  it('does not send a main database connection request when form validation fails', async () => {
    validationResults.main_db = false
    const { wrapper } = await mountSettings()
    await findCardButton(wrapper, 0, '编辑').trigger('click')
    await findButton(wrapper, '测试连接').trigger('click')
    await flushPromises()

    expect(requestCalls.filter((call) => call.method === 'post')).toEqual([])
    expect(mocks.success).not.toHaveBeenCalled()
  })

  it('tests the main database using its endpoint and edited credentials, then reports success', async () => {
    const { wrapper } = await mountSettings()
    await findCardButton(wrapper, 0, '编辑').trigger('click')
    await field(wrapper, 'main_db', 'password').setValue('C32_MAIN_TEST_PASSWORD')
    await findButton(wrapper, '测试连接').trigger('click')
    await flushPromises()

    expect(requestCalls.find((call) => call.method === 'post')).toMatchObject({
      url: '/api/v1/settings/database/test',
      method: 'post',
      body: {
        host: MAIN_CONFIG.host,
        port: MAIN_CONFIG.port,
        database: MAIN_CONFIG.database,
        user: MAIN_CONFIG.user,
        password: 'C32_MAIN_TEST_PASSWORD',
      },
    })
    expect(mocks.success).toHaveBeenCalledWith('主数据库连接成功')
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('tests the warehouse using its endpoint and reports the server rejection', async () => {
    answer = (call) => {
      if (call.method === 'post' && call.url === '/api/v1/settings/database/warehouse/test') {
        return success({ success: false, message: 'C32_WAREHOUSE_TEST_REJECTED' })
      }
      return defaultAnswer(call)
    }
    const { wrapper } = await mountSettings()
    await findCardButton(wrapper, 1, '编辑').trigger('click')
    await field(wrapper, 'warehouse_db', 'password').setValue('C32_WAREHOUSE_TEST_PASSWORD')
    await findButton(wrapper, '测试连接').trigger('click')
    await flushPromises()

    expect(requestCalls.find((call) => call.method === 'post')).toMatchObject({
      url: '/api/v1/settings/database/warehouse/test',
      method: 'post',
      body: {
        host: WAREHOUSE_CONFIG.host,
        port: WAREHOUSE_CONFIG.port,
        database: WAREHOUSE_CONFIG.database,
        user: WAREHOUSE_CONFIG.user,
        password: 'C32_WAREHOUSE_TEST_PASSWORD',
      },
    })
    expect(mocks.error).toHaveBeenCalledWith('连接失败: C32_WAREHOUSE_TEST_REJECTED')
    expect(mocks.success).not.toHaveBeenCalled()
    expect(flat(wrapper)).toContain('数据仓库:未连接')
  })

  it('shows the not-implemented save warning and never sends a PUT', async () => {
    const { wrapper } = await mountSettings()
    await findCardButton(wrapper, 0, '编辑').trigger('click')
    await field(wrapper, 'main_db', 'text').setValue('C32_UNSAVED_HOST')
    await findCardButton(wrapper, 0, '保存').trigger('click')

    expect(flat(wrapper)).toContain('保存配置警告')
    expect(flat(wrapper)).toContain('请确保您已保存所有重要工作')
    await findButton(wrapper, '确认保存').trigger('click')
    await flushPromises()

    expect(mocks.warning).toHaveBeenCalledWith(
      '配置更新功能暂未实现。请手动更新 .env 文件并重启服务。'
    )
    expect(requestCalls.some((call) => call.method === 'put')).toBe(false)
    expect(mocks.success).not.toHaveBeenCalled()
    expect((field(wrapper, 'main_db', 'text').element as HTMLInputElement).value).toBe(
      MAIN_CONFIG.host
    )
  })
})
