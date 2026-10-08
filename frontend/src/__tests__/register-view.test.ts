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
import RegisterView from '@/views/RegisterView.vue'
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
    return () => h('form', { 'data-testid': 'register-form' }, slots.default?.())
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
      { path: '/register', component: RegisterView },
      { path: '/login', component: DestinationStub },
      { path: '/', component: DestinationStub },
      { path: '/:pathMatch(.*)*', component: DestinationStub },
    ],
  })
}

async function mountRegister() {
  const pinia = createPinia()
  pinia.use(piniaPluginPersistedstate)
  setActivePinia(pinia)
  const router = makeRouter()
  await router.push('/register')
  await router.isReady()
  const wrapper = mount(RegisterView, {
    global: { plugins: [pinia, router], stubs },
  })
  return { wrapper, router, pinia }
}

function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === text)
  if (!button) throw new Error(`Missing button: ${text}`)
  return button
}

async function fillRegistration(
  wrapper: VueWrapper,
  email = 'new-user@example.com',
  password = 'strong-pass-42',
  confirmation = password
) {
  await wrapper.get('input[placeholder="请输入邮箱"]').setValue(email)
  await wrapper.get('input[placeholder="请输入密码（至少8位）"]').setValue(password)
  await wrapper.get('input[placeholder="请再次输入密码"]').setValue(confirmation)
}

describe('RegisterView with the real auth store and API wrapper', () => {
  it('posts the wire registration contract, loads /me, then navigates to login', async () => {
    const { wrapper, router } = await mountRegister()
    respond = (call) => {
      if (call.url === '/api/v1/auth/register') {
        expect(call.method).toBe('post')
        expect(call.body).toEqual({
          email: 'new-user@example.com',
          password: 'strong-pass-42',
          password_confirm: 'strong-pass-42',
        })
        return {
          success: true,
          message: '注册成功',
          data: {
            user_id: 91,
            email: 'new-user@example.com',
            access_token: 'access.register.fixture',
            refresh_token: 'refresh.register.fixture',
          },
        }
      }
      if (call.url === '/api/v1/auth/me') {
        expect(call.method).toBe('get')
        expect(call.authorization).toBe('Bearer access.register.fixture')
        return {
          success: true,
          data: {
            user_id: 91,
            email: 'new-user@example.com',
            role: 'user',
            is_active: true,
            created_at: '2026-10-08T00:00:00Z',
            updated_at: null,
          },
        }
      }
      throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
    }

    await fillRegistration(wrapper)
    await buttonByText(wrapper, '注册').trigger('click')
    await flushPromises()

    expect(calls.map(({ method, url }) => ({ method, url }))).toEqual([
      { method: 'post', url: '/api/v1/auth/register' },
      { method: 'get', url: '/api/v1/auth/me' },
    ])
    expect(router.currentRoute.value.fullPath).toBe('/login')
    expect(useAuthStore().accessToken).toBe('access.register.fixture')
    expect(useAuthStore().isAuthenticated).toBe(true)
    expect((useAuthStore().user as unknown as Record<string, unknown>).user_id).toBe(91)
    expect(useAuthStore().user).not.toHaveProperty('id')
    expect(messages.success).toHaveBeenCalled()
  })

  it('does not call the API when required, length, or confirmation validation fails', async () => {
    const { wrapper, router } = await mountRegister()

    await fillRegistration(wrapper, '', 'short', '')
    await buttonByText(wrapper, '注册').trigger('click')
    await flushPromises()
    expect(calls).toHaveLength(0)
    expect(router.currentRoute.value.fullPath).toBe('/register')

    await fillRegistration(wrapper, 'new-user@example.com', 'strong-pass-42', 'different-pass-42')
    await buttonByText(wrapper, '注册').trigger('click')
    await flushPromises()

    expect(calls).toHaveLength(0)
    expect(router.currentRoute.value.fullPath).toBe('/register')
    expect(useAuthStore().isAuthenticated).toBe(false)
  })

  it('shows a registration failure and allows retry with the same form values', async () => {
    const { wrapper, router } = await mountRegister()
    let shouldFail = true
    respond = (call) => {
      if (call.url === '/api/v1/auth/register') {
        if (shouldFail) return { success: false, message: '邮箱已被使用' }
        return {
          success: true,
          data: {
            user_id: 92,
            email: 'new-user@example.com',
            access_token: 'access.retry.fixture',
            refresh_token: 'refresh.retry.fixture',
          },
        }
      }
      if (call.url === '/api/v1/auth/me') {
        return {
          success: true,
          data: {
            user_id: 92,
            email: 'new-user@example.com',
            role: 'user',
            is_active: true,
            created_at: '2026-10-08T00:00:00Z',
            updated_at: null,
          },
        }
      }
      throw new Error(`Unexpected auth request: ${call.method} ${call.url}`)
    }

    await fillRegistration(wrapper)
    await buttonByText(wrapper, '注册').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/register')
    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(messages.error).toHaveBeenCalledWith('邮箱已被使用')
    expect(messages.success).not.toHaveBeenCalled()

    shouldFail = false
    await buttonByText(wrapper, '注册').trigger('click')
    await flushPromises()

    expect(calls.filter((call) => call.url === '/api/v1/auth/register')).toHaveLength(2)
    expect(calls.filter((call) => call.url === '/api/v1/auth/me')).toHaveLength(1)
    expect(router.currentRoute.value.fullPath).toBe('/login')
    expect(useAuthStore().isAuthenticated).toBe(true)
    expect(messages.success).toHaveBeenCalled()
  })

  it('navigates to login from the existing-account link', async () => {
    const { wrapper, router } = await mountRegister()

    await buttonByText(wrapper, '已有账户？立即登录').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/login')
    expect(calls).toHaveLength(0)
  })
})
