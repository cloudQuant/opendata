import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { AxiosError, type AxiosAdapter, type AxiosResponse } from 'axios'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import App from '@/App.vue'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'
import request from '@/utils/request'

const adapterCalls: Array<{
  url: string
  method: string
  authorization: string | null
  data: unknown
}> = []
const routerPushCalls: unknown[] = []
const routerPushPromises: Promise<unknown>[] = []

const script = {
  id: 7,
  script_id: '7',
  script_name: 'AC21 router counterfact',
  category: 'AC21',
  sub_category: null,
  frequency: 'manual',
  description: 'Synthetic router counterfact',
  source: 'ac21-test-adapter',
  target_table: null,
  module_path: 'ac21.fixture',
  function_name: 'fetch_rows',
  parameters: [],
  estimated_duration: 1,
  timeout: 5,
  is_active: true,
  is_custom: false,
  created_at: '2026-10-12T00:00:00Z',
  updated_at: '2026-10-12T00:00:00Z',
}

const interfacePage = {
  items: [
    {
      id: 91,
      name: 'ac21_selected_interface',
      display_name: 'AC21_SELECTED_INTERFACE_91',
      description: 'Synthetic selected interface',
      category_name: 'AC21',
      is_active: true,
    },
  ],
  total: 1,
  page: 1,
  page_size: 100,
  total_pages: 1,
}

const executionPage = {
  items: [
    {
      id: 901,
      execution_id: 'dl_ac21_router_counterfact',
      task_id: null,
      script_id: '7',
      status: 'completed',
      start_time: '2026-10-12T00:00:00Z',
      end_time: '2026-10-12T00:00:01Z',
      duration: 1,
      error_message: null,
      rows_before: 0,
      rows_after: 2,
      retry_count: 0,
      triggered_by: 'manual',
      created_at: '2026-10-12T00:00:00Z',
    },
  ],
  total: 1,
  page: 1,
  page_size: 20,
  total_pages: 1,
}

function response(config: Parameters<AxiosAdapter>[0], body: unknown, status = 200): AxiosResponse {
  const result = {
    data: { success: true, data: body },
    status,
    statusText: status === 202 ? 'Accepted' : 'OK',
    headers: {},
    config,
    request: {},
  } as AxiosResponse
  return result
}

const realClientAdapter: AxiosAdapter = async (config) => {
  const url = config.url ?? ''
  const method = (config.method ?? 'get').toLowerCase()
  const authorization = String(config.headers?.Authorization ?? '') || null
  adapterCalls.push({ url, method, authorization, data: config.data })

  let body: unknown
  let status = 200
  if (url === '/scripts/7' && method === 'get') body = script
  else if (url === '/data/interfaces/' && method === 'get') body = interfacePage
  else if (url === '/data/download' && method === 'post') {
    body = { execution_id: 901, status: 'pending', message: 'Download queued' }
    status = 202
  } else if (url === '/executions/' && method === 'get') body = executionPage
  else if (url === '/executions/stats' && method === 'get') {
    body = {
      total_count: 1,
      success_count: 1,
      failed_count: 0,
      success_rate: 1,
      avg_duration: 1,
      today_executions: 1,
    }
  } else if (url === '/pipeline/failures' && method === 'get') {
    body = { count: 0, failures: [] }
  } else {
    throw new Error(`Unexpected counterfact request: ${method.toUpperCase()} ${url}`)
  }

  const result = response(config, body, status)
  if (config.validateStatus && !config.validateStatus(status)) {
    throw new AxiosError(
      `Request failed with status code ${status}`,
      AxiosError.ERR_BAD_REQUEST,
      config,
      {},
      result
    )
  }
  return result
}

const originalAdapter = request.defaults.adapter
let wrapper: VueWrapper | undefined
let pinia = createPinia()
let i18n: (typeof import('@/i18n'))['default']
let removeAfterEach: (() => void) | undefined
let removeOnError: (() => void) | undefined
let originalRouterPush: typeof router.push | undefined
const routeEvents: Array<{ from: string; to: string; failure: string | null }> = []
const routerErrors: string[] = []

describe('actual script download route and Axios response interceptor', () => {
  beforeEach(async () => {
    adapterCalls.length = 0
    routerPushCalls.length = 0
    routerPushPromises.length = 0
    routeEvents.length = 0
    routerErrors.length = 0
    const values = new Map<string, string>()
    const storage = {
      get length() {
        return values.size
      },
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      key: (index: number) => Array.from(values.keys())[index] ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, String(value)),
    } as Storage
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
    i18n = (await import('@/i18n')).default
    pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAuthStore()
    auth.user = {
      user_id: 71001,
      email: 'ac21-router@example.invalid',
      role: 'admin',
      is_active: true,
      created_at: '2026-10-12T00:00:00Z',
      updated_at: null,
    }
    auth.accessToken = 'ac21-router-session-token'
    auth.refreshToken = null
    request.defaults.adapter = realClientAdapter
    removeAfterEach = router.afterEach((to, from, failure) => {
      routeEvents.push({
        from: from.fullPath,
        to: to.fullPath,
        failure: failure ? String(failure) : null,
      })
    })
    removeOnError = router.onError((error) => routerErrors.push(String(error.message)))
    originalRouterPush = router.push
    const actualPush = router.push.bind(router)
    router.push = ((to: Parameters<typeof router.push>[0]) => {
      routerPushCalls.push(to)
      const pending = actualPush(to)
      routerPushPromises.push(pending)
      return pending
    }) as typeof router.push
    await router.replace('/scripts/7')
    await router.isReady()
  })

  afterEach(async () => {
    wrapper?.unmount()
    wrapper = undefined
    removeAfterEach?.()
    removeOnError?.()
    removeAfterEach = undefined
    removeOnError = undefined
    if (originalRouterPush) router.push = originalRouterPush
    originalRouterPush = undefined
    request.defaults.adapter = originalAdapter
    setActivePinia(undefined)
  })

  it('resolves the production lazy ExecutionsView loader', async () => {
    const route = router.getRoutes().find((candidate) => candidate.name === 'Executions')
    const loader = route?.components?.default
    expect(loader).toBeTypeOf('function')
    const component = await (loader as () => Promise<unknown>)()
    expect(component).toBeDefined()
  })

  it('uses the real detail route, 202 response interceptor, and lazy executions route', async () => {
    wrapper = mount(App, {
      global: { plugins: [pinia, ElementPlus, i18n, router] },
    })
    await flushPromises()
    expect(wrapper.find('.script-detail-view').exists()).toBe(true)
    expect(wrapper.text()).toContain('AC21 router counterfact')

    const select = wrapper.findComponent({ name: 'ElSelect' })
    expect(select.exists()).toBe(true)
    await select.vm.$emit('update:modelValue', 91)
    await flushPromises()

    const button = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().trim() === '创建下载任务')
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
    const navigationSettled = await Promise.race([
      routerPushPromises[routerPushPromises.length - 1]!.then(
        (result) => ({ settled: true, result }),
        (error: unknown) => ({ settled: true, error: String(error) })
      ),
      new Promise<{ settled: false }>((resolve) => {
        window.setTimeout(() => resolve({ settled: false }), 5000)
      }),
    ])
    expect(navigationSettled, JSON.stringify({ routeEvents, routerErrors })).toMatchObject({
      settled: true,
    })
    await flushPromises()

    const downloadCall = adapterCalls.find(
      (call) => call.url === '/data/download' && call.method === 'post'
    )
    expect(
      downloadCall,
      JSON.stringify({ calls: adapterCalls, text: wrapper.text() })
    ).toBeDefined()
    expect(
      router.currentRoute.value.path,
      JSON.stringify({
        routeEvents,
        routerErrors,
        routerPushCalls,
        text: wrapper.text(),
        bodyText: document.body.textContent,
      })
    ).toBe('/executions')
    expect(wrapper.find('.executions-view').exists()).toBe(true)
    expect(wrapper.text()).toContain('成功')
    expect(routeEvents).toContainEqual({
      from: '/scripts/7',
      to: '/executions',
      failure: null,
    })
    expect(routerErrors).toEqual([])

    expect(downloadCall?.authorization).toBe('Bearer ac21-router-session-token')
    expect(JSON.parse(String(downloadCall?.data))).toEqual({ interface_id: 91, parameters: {} })
    expect(adapterCalls.map((call) => call.url)).toEqual(
      expect.arrayContaining(['/executions/', '/executions/stats', '/pipeline/failures'])
    )
  })
})
