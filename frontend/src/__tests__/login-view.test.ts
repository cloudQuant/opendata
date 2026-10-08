/* eslint vue/one-component-per-file: off */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { enableAutoUnmount, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, type PropType } from 'vue'
import Schema, { type Rules } from 'async-validator'
import type { FormRules } from 'element-plus'
import request from '@/utils/request'
import LoginView from '@/views/LoginView.vue'
import { useAuthStore } from '@/stores/auth'

const messages = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}))

vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>('element-plus')
  return {
    ...actual,
    ElMessage: {
      success: messages.success,
      error: messages.error,
    },
  }
})

type WireEnvelope = { success: boolean; message?: string; data?: unknown }

interface RequestCall {
  url: string
  method: string
  body: unknown
  authorization: string | undefined
}

const calls: RequestCall[] = []
let respond: (call: RequestCall) => WireEnvelope | Promise<WireEnvelope>
const originalAdapter = request.defaults.adapter
const originalLocalStorageDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage')

function createTestStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  }
}

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
    body: readBody(config.data),
    authorization: config.headers?.Authorization as string | undefined,
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

const CardStub = defineComponent({
  setup(_props, { slots }) {
    return () => h('section', [slots.header?.(), slots.default?.()])
  },
})

const FormStub = defineComponent({
  props: {
    model: { type: Object as PropType<Record<string, unknown>>, required: true },
    rules: { type: Object as PropType<FormRules>, default: () => ({}) },
  },
  setup(props, { expose, slots }) {
    async function validate() {
      try {
        await new Schema(props.rules as unknown as Rules).validate(props.model)
      } catch (error) {
        const firstError =
          error && typeof error === 'object' && 'errors' in error
            ? (error as { errors?: Array<{ message?: string }> }).errors?.[0]?.message
            : undefined
        throw new Error(firstError ?? '表单验证失败')
      }
    }
    expose({ validate })
    return () => h('form', { 'data-testid': 'login-form' }, slots.default?.())
  },
})

const FormItemStub = defineComponent({
  setup(_props, { slots }) {
    return () => h('div', slots.default?.())
  },
})

const InputStub = defineComponent({
  props: {
    modelValue: { type: String, default: '' },
    type: { type: String, default: 'text' },
    placeholder: { type: String, default: '' },
    disabled: { type: Boolean, default: false },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    return () =>
      h('input', {
        type: props.type,
        placeholder: props.placeholder,
        value: props.modelValue,
        disabled: props.disabled,
        onInput: (event: Event) =>
          emit('update:modelValue', (event.target as HTMLInputElement).value),
      })
  },
})

const ButtonStub = defineComponent({
  props: {
    loading: { type: Boolean, default: false },
    disabled: { type: Boolean, default: false },
  },
  setup(props, { attrs, slots }) {
    return () =>
      h(
        'button',
        {
          ...attrs,
          type: 'button',
          disabled: props.disabled || props.loading,
          'aria-busy': props.loading ? 'true' : 'false',
        },
        slots.default?.()
      )
  },
})

const DestinationStub = defineComponent({
  setup() {
    return () => h('div', { 'data-testid': 'destination' })
  },
})

const stubs = {
  'el-card': CardStub,
  'el-form': FormStub,
  'el-form-item': FormItemStub,
  'el-input': InputStub,
  'el-button': ButtonStub,
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  calls.length = 0
  respond = (call) => {
    throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
  }
  request.defaults.adapter = adapter
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: createTestStorage(),
  })
  messages.success.mockClear()
  messages.error.mockClear()

  const pinia = createPinia()
  pinia.use(piniaPluginPersistedstate)
  setActivePinia(pinia)
})

afterEach(() => {
  request.defaults.adapter = originalAdapter
  if (originalLocalStorageDescriptor) {
    Object.defineProperty(window, 'localStorage', originalLocalStorageDescriptor)
  } else {
    Reflect.deleteProperty(window, 'localStorage')
  }
  vi.clearAllMocks()
})

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', component: LoginView },
      { path: '/register', component: DestinationStub },
      { path: '/', component: DestinationStub },
      { path: '/datasets', component: DestinationStub },
      { path: '/:pathMatch(.*)*', component: DestinationStub },
    ],
  })
}

async function mountLogin(path = '/login') {
  const pinia = createPinia()
  pinia.use(piniaPluginPersistedstate)
  setActivePinia(pinia)
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const wrapper = mount(LoginView, {
    global: { plugins: [pinia, router], stubs },
  })
  return { wrapper, router, pinia }
}

function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === text)
  if (!button) throw new Error(`Missing button: ${text}`)
  return button
}

async function fillCredentials(wrapper: VueWrapper, email = 'analyst@example.com', password = 'secret-pass-42') {
  await wrapper.get('input[placeholder="请输入邮箱"]').setValue(email)
  await wrapper.get('input[placeholder="请输入密码"]').setValue(password)
}

describe('LoginView with the real auth store and API wrapper', () => {
  it('posts the entered credentials and follows the requested redirect', async () => {
    const { wrapper, router } = await mountLogin('/login?redirect=%2Fdatasets')
    respond = (call) => {
      expect(call.url).toBe('/api/v1/auth/login')
      expect(call.method).toBe('post')
      expect(call.body).toEqual({ email: 'analyst@example.com', password: 'secret-pass-42' })
      return {
        success: true,
        message: '登录成功',
        data: {
          access_token: 'access.login.fixture',
          refresh_token: 'refresh.login.fixture',
          user: {
            user_id: 27,
            email: 'analyst@example.com',
            role: 'user',
            created_at: '2026-10-08T00:00:00Z',
            updated_at: null,
          },
        },
      }
    }

    await fillCredentials(wrapper)
    await buttonByText(wrapper, '登录').trigger('click')
    await flushPromises()

    const authStore = useAuthStore()
    expect(calls).toHaveLength(1)
    expect(router.currentRoute.value.fullPath).toBe('/datasets')
    expect(authStore.accessToken).toBe('access.login.fixture')
    expect(authStore.refreshToken).toBe('refresh.login.fixture')
    expect(authStore.isAuthenticated).toBe(true)
    expect((authStore.user as unknown as Record<string, unknown>).user_id).toBe(27)
    expect(authStore.user).not.toHaveProperty('id')
    expect(messages.success).toHaveBeenCalled()
  })

  it('uses the home route when no redirect was requested', async () => {
    const { wrapper, router } = await mountLogin()
    respond = () => ({
      success: true,
      data: {
        access_token: 'access.default.fixture',
        refresh_token: 'refresh.default.fixture',
        user: {
          user_id: 28,
          email: 'analyst@example.com',
          role: 'admin',
          created_at: '2026-10-08T00:00:00Z',
          updated_at: '2026-10-08T00:00:00Z',
        },
      },
    })

    await fillCredentials(wrapper)
    await buttonByText(wrapper, '登录').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/')
    expect(calls).toHaveLength(1)
    expect(useAuthStore().isAdmin).toBe(true)
  })

  it('keeps inputs and submit disabled while the login request is pending', async () => {
    let resolveResponse!: (envelope: WireEnvelope) => void
    respond = () => new Promise<WireEnvelope>((resolve) => { resolveResponse = resolve })
    const { wrapper, router } = await mountLogin('/login?redirect=%2Fdatasets')

    await fillCredentials(wrapper)
    const submit = buttonByText(wrapper, '登录')
    await submit.trigger('click')
    await flushPromises()

    expect(calls).toHaveLength(1)
    expect(submit.attributes('disabled')).toBeDefined()
    expect(submit.attributes('aria-busy')).toBe('true')
    expect(wrapper.get('input[placeholder="请输入邮箱"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('input[placeholder="请输入密码"]').attributes('disabled')).toBeDefined()
    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=%2Fdatasets')

    resolveResponse({
      success: true,
      data: {
        access_token: 'access.pending.fixture',
        refresh_token: 'refresh.pending.fixture',
        user: {
          user_id: 29,
          email: 'analyst@example.com',
          role: 'user',
          created_at: '2026-10-08T00:00:00Z',
          updated_at: null,
        },
      },
    })
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/datasets')
    expect(buttonByText(wrapper, '登录').attributes('disabled')).toBeUndefined()
  })

  it('shows an API failure and stays on the login route without authenticating', async () => {
    const { wrapper, router } = await mountLogin('/login?redirect=%2Fdatasets')
    respond = () => ({ success: false, message: '账号或密码错误' })

    await fillCredentials(wrapper)
    await buttonByText(wrapper, '登录').trigger('click')
    await flushPromises()

    expect(calls).toHaveLength(1)
    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=%2Fdatasets')
    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(messages.error).toHaveBeenCalledWith('账号或密码错误')
    expect(messages.success).not.toHaveBeenCalled()
  })

  it.each([
    { email: 'not-an-email', password: 'secret-pass-42' },
    { email: 'analyst@example.com', password: 'short' },
  ])('does not call the API for invalid credentials %#', async ({ email, password }) => {
    const { wrapper, router } = await mountLogin()

    await fillCredentials(wrapper, email, password)
    await buttonByText(wrapper, '登录').trigger('click')
    await flushPromises()

    expect(calls).toHaveLength(0)
    expect(router.currentRoute.value.fullPath).toBe('/login')
    expect(useAuthStore().isAuthenticated).toBe(false)
  })

  it('navigates to registration from the visible account link', async () => {
    const { wrapper, router } = await mountLogin()

    await buttonByText(wrapper, '还没有账户？立即注册').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/register')
    expect(calls).toHaveLength(0)
  })
})
