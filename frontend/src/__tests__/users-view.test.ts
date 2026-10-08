/* eslint vue/one-component-per-file: off */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, type PropType, type VNode } from 'vue'
import request from '@/utils/request'
import { useAuthStore } from '@/stores/auth'
import UsersView from '@/views/UsersView.vue'
import type { AuthUser, PaginatedResponse, User } from '@/types'

const mocks = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  confirm: vi.fn(),
  prompt: vi.fn(),
}))

vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>('element-plus')
  return {
    ...actual,
    ElMessage: { success: mocks.success, error: mocks.error },
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

const ADMIN = user(1, 'C32_ADMIN@example.test', 'admin')
const TARGET = user(24, 'C32_TARGET@example.test', 'user')
const PAGE_TWO = user(25, 'C32_PAGE_TWO@example.test', 'user')

function user(id: number, email: string, role: User['role']): User {
  return {
    id,
    email,
    role,
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}

function authUser(role: User['role'] = 'admin'): AuthUser {
  return {
    user_id: ADMIN.id,
    email: ADMIN.email,
    role,
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: null,
  }
}

const requestCalls: RequestCall[] = []
let answer: (call: RequestCall) => WireEnvelope | Promise<WireEnvelope>
let currentTargetRole: User['role'] = 'user'
let deletedUserIds = new Set<number>()
let failedMutation: string | null = null
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

function listedUsers(): User[] {
  return [
    ADMIN,
    user(TARGET.id, TARGET.email, currentTargetRole),
    ...Array.from({ length: 18 }, (_, index) =>
      user(index + 3, `C32_USER_${index + 3}@example.test`, 'user')
    ),
    PAGE_TWO,
    ...Array.from({ length: 4 }, (_, index) =>
      user(index + 26, `C32_PAGE_TWO_${index + 26}@example.test`, 'user')
    ),
  ].filter((item) => !deletedUserIds.has(item.id))
}

function pageResponse(page: number): WireEnvelope {
  const rows = listedUsers()
  const pageSize = 20
  const items = rows.slice((page - 1) * pageSize, page * pageSize)
  return success({
    items,
    total: rows.length,
    page,
    page_size: pageSize,
  } satisfies PaginatedResponse<User>)
}

function defaultAnswer(call: RequestCall): WireEnvelope {
  if (call.method === 'get' && call.url === '/api/v1/users/') {
    const page = (call.params as { page?: number } | undefined)?.page ?? 1
    return pageResponse(page)
  }

  if (call.method === 'put' && call.url === `/api/v1/users/${TARGET.id}`) {
    if (failedMutation === 'put') {
      return { success: false, message: 'C32_ROLE_UPDATE_REJECTED' }
    }
    const role = (call.body as { role: User['role'] }).role
    currentTargetRole = role
    return success(user(TARGET.id, TARGET.email, role))
  }

  if (call.method === 'delete' && call.url === `/api/v1/users/${TARGET.id}`) {
    if (failedMutation === 'delete') {
      return { success: false, message: 'C32_USER_DELETE_REJECTED' }
    }
    deletedUserIds.add(TARGET.id)
    return success(null)
  }

  return { success: false, message: `C32_UNEXPECTED_REQUEST_${call.method}_${call.url}` }
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
      { path: '/users', component: DestinationView },
    ],
  })
}

function columnSlots(node: VNode, row: User, index: number) {
  const children = node.children as {
    default?: (scope: { row: unknown; $index: number }) => VNode[]
  } | null
  return children?.default?.({ row, $index: index })
}

const stubs = {
  ElCard: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => {
        const header = slots.header?.()
        const body = slots.default?.()
        return h('section', attrs, [
          header?.length ? h('header', header) : null,
          h('div', { class: 'card-body' }, body),
        ])
      }
    },
  }),
  ElInput: defineComponent({
    inheritAttrs: false,
    props: {
      modelValue: { type: String, default: '' },
      placeholder: { type: String, default: '' },
    },
    emits: ['update:modelValue'],
    setup(props, { attrs, emit }) {
      return () =>
        h('input', {
          ...attrs,
          value: props.modelValue,
          placeholder: props.placeholder,
          onInput: (event: Event) =>
            emit('update:modelValue', (event.target as HTMLInputElement).value),
        })
    },
  }),
  ElIcon: defineComponent({
    setup(_props, { slots }) {
      return () => h('span', slots.default?.())
    },
  }),
  Search: defineComponent({
    setup() {
      return () => h('span', { 'data-testid': 'search-icon' })
    },
  }),
  ElTable: defineComponent({
    props: { data: { type: Array as PropType<User[]>, default: () => [] } },
    setup(props, { slots }) {
      return () => {
        const columns = slots.default?.() ?? []
        return h(
          'table',
          { 'data-testid': 'users-table' },
          h(
            'tbody',
            props.data.map((row, index) =>
              h(
                'tr',
                { 'data-user-id': row.id },
                columns.map((column) => {
                  const content = columnSlots(column, row, index)
                  if (content) return h('td', content)
                  const prop = column.props?.prop as keyof User | undefined
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
  ElTag: defineComponent({
    setup(_props, { attrs, slots }) {
      return () => h('span', attrs, slots.default?.())
    },
  }),
  ElButton: defineComponent({
    inheritAttrs: false,
    props: { disabled: { type: Boolean, default: false } },
    emits: ['click'],
    setup(props, { attrs, emit, slots }) {
      return () =>
        h(
          'button',
          {
            ...attrs,
            type: 'button',
            disabled: props.disabled || attrs.disabled,
            onClick: (event: Event) => emit('click', event),
          },
          slots.default?.()
        )
    },
  }),
  ElPagination: defineComponent({
    props: {
      currentPage: { type: Number, default: 1 },
      pageSize: { type: Number, default: 20 },
      total: { type: Number, default: 0 },
    },
    emits: ['current-change'],
    setup(props, { emit }) {
      return () => {
        const pageCount = Math.ceil(props.total / props.pageSize)
        return h('nav', { 'data-testid': 'users-pagination' }, [
          h('span', `总计 ${props.total} 条`),
          ...Array.from({ length: pageCount }, (_, index) => {
            const page = index + 1
            return h(
              'button',
              {
                type: 'button',
                'data-page': page,
                'aria-current': page === props.currentPage ? 'page' : undefined,
                onClick: () => emit('current-change', page),
              },
              String(page)
            )
          }),
        ])
      }
    },
  }),
  ElEmpty: defineComponent({
    props: { description: { type: String, default: '' } },
    setup(props) {
      return () => h('div', { 'data-testid': 'no-permission' }, props.description)
    },
  }),
}

function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ').trim()
}

function rowFor(wrapper: ReturnType<typeof mount>, email: string) {
  const row = wrapper.findAll('tbody tr').find((candidate) => flat(candidate).includes(email))
  expect(row, `row for ${email}`).toBeDefined()
  return row!
}

function rowButton(wrapper: ReturnType<typeof mount>, email: string, label: string) {
  const row = rowFor(wrapper, email)
  const button = row.findAll('button').find((candidate) => candidate.text().trim() === label)
  expect(button, `button named ${label} in row ${email}`).toBeDefined()
  return button!
}

function allCalls(method: string, url: string) {
  return requestCalls.filter((call) => call.method === method && call.url === url)
}

async function mountUsers(role: User['role'] = 'admin') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = authUser(role)
  auth.accessToken = 'C32_USERS_ACCESS_TOKEN'

  const router = testRouter()
  await router.push('/users')
  await router.isReady()
  const wrapper = mount(UsersView, { global: { plugins: [pinia, router], stubs } })
  await flushPromises()
  return { wrapper, router }
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  requestCalls.length = 0
  answer = defaultAnswer
  currentTargetRole = 'user'
  deletedUserIds = new Set()
  failedMutation = null
  request.defaults.adapter = adapter
  mocks.success.mockReset()
  mocks.error.mockReset()
  mocks.confirm.mockReset()
  mocks.prompt.mockReset()
  mocks.confirm.mockResolvedValue('confirm')
  mocks.prompt.mockResolvedValue({ value: 'admin' })
})

afterEach(() => {
  request.defaults.adapter = originalAdapter
})

describe('UsersView administrator flow', () => {
  it('redirects a non-admin without requesting the user list', async () => {
    const { wrapper, router } = await mountUsers('user')

    expect(router.currentRoute.value.fullPath).toBe('/')
    expect(flat(wrapper)).toContain('您没有权限访问此页面')
    expect(requestCalls).toEqual([])
    expect(mocks.error).toHaveBeenCalledWith('权限不足')
  })

  it('loads the first page with pagination params, filters visible email rows, and requests page two', async () => {
    const { wrapper } = await mountUsers()

    expect(allCalls('get', '/api/v1/users/')).toHaveLength(1)
    expect(allCalls('get', '/api/v1/users/')[0].params).toEqual({ page: 1, page_size: 20 })
    expect(flat(rowFor(wrapper, ADMIN.email))).toContain('admin')
    expect(flat(rowFor(wrapper, TARGET.email))).toContain('user')

    const search = wrapper.find('input[placeholder="搜索邮箱"]')
    await search.setValue('C32_target@EXAMPLE.test')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(flat(wrapper.find('tbody tr'))).toContain(TARGET.email)
    expect(allCalls('get', '/api/v1/users/')).toHaveLength(1)

    await search.setValue('')
    await wrapper.find('button[data-page="2"]').trigger('click')
    await flushPromises()

    expect(allCalls('get', '/api/v1/users/')).toHaveLength(2)
    expect(allCalls('get', '/api/v1/users/')[1].params).toEqual({ page: 2, page_size: 20 })
    expect(flat(rowFor(wrapper, PAGE_TWO.email))).toContain('C32_PAGE_TWO')
  })

  it('updates the selected user id and role, then reloads the current page', async () => {
    const { wrapper } = await mountUsers()
    mocks.prompt.mockResolvedValueOnce({ value: 'admin' })

    await rowButton(wrapper, TARGET.email, '修改角色').trigger('click')
    await flushPromises()

    expect(mocks.prompt).toHaveBeenCalledWith(
      '请选择用户角色',
      '修改角色',
      expect.objectContaining({ inputValue: 'user' })
    )
    expect(allCalls('put', `/api/v1/users/${TARGET.id}`)).toHaveLength(1)
    expect(allCalls('put', `/api/v1/users/${TARGET.id}`)[0].body).toEqual({ role: 'admin' })
    expect(allCalls('get', '/api/v1/users/')).toHaveLength(2)
    expect(flat(rowFor(wrapper, TARGET.email))).toContain('admin')
    expect(mocks.success).toHaveBeenCalledWith('角色更新成功')
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('does not send a role update when the prompt is cancelled', async () => {
    const { wrapper } = await mountUsers()
    mocks.prompt.mockRejectedValueOnce('cancel')

    await rowButton(wrapper, TARGET.email, '修改角色').trigger('click')
    await flushPromises()

    expect(allCalls('put', `/api/v1/users/${TARGET.id}`)).toEqual([])
    expect(allCalls('get', '/api/v1/users/')).toHaveLength(1)
    expect(mocks.success).not.toHaveBeenCalled()
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('deletes the selected user id and reloads the list after success', async () => {
    const { wrapper } = await mountUsers()
    await rowButton(wrapper, TARGET.email, '删除').trigger('click')
    await flushPromises()

    expect(mocks.confirm).toHaveBeenCalledWith(
      `确定要删除用户 ${TARGET.email} 吗？`,
      '确认删除',
      expect.objectContaining({ type: 'warning' })
    )
    expect(allCalls('delete', `/api/v1/users/${TARGET.id}`)).toHaveLength(1)
    expect(allCalls('get', '/api/v1/users/')).toHaveLength(2)
    expect(wrapper.findAll('tbody tr').some((row) => flat(row).includes(TARGET.email))).toBe(false)
    expect(mocks.success).toHaveBeenCalledWith('删除成功')
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('does not offer a delete action for the signed-in administrator', async () => {
    const { wrapper } = await mountUsers()
    const selfRow = rowFor(wrapper, ADMIN.email)
    const labels = selfRow.findAll('button').map((button) => button.text().trim())

    expect(labels).toContain('修改角色')
    expect(labels).not.toContain('删除')
    expect(allCalls('delete', `/api/v1/users/${ADMIN.id}`)).toEqual([])
  })

  it('shows a failed delete response without claiming success or refreshing the list', async () => {
    failedMutation = 'delete'
    const { wrapper } = await mountUsers()

    await rowButton(wrapper, TARGET.email, '删除').trigger('click')
    await flushPromises()

    expect(allCalls('delete', `/api/v1/users/${TARGET.id}`)).toHaveLength(1)
    expect(allCalls('get', '/api/v1/users/')).toHaveLength(1)
    expect(flat(rowFor(wrapper, TARGET.email))).toContain(TARGET.email)
    expect(mocks.error).toHaveBeenCalledWith('C32_USER_DELETE_REJECTED')
    expect(mocks.success).not.toHaveBeenCalled()
  })
})
