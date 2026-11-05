import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { h } from 'vue'
import LayoutView from '@/views/LayoutView.vue'
import i18n from '@/i18n'

const authState = vi.hoisted(() => ({
  isAuthenticated: false,
  isAdmin: false,
  user: { email: 'admin@example.com', role: 'admin' },
  logout: vi.fn(),
}))

vi.mock('@/stores/auth', () => ({ useAuthStore: () => authState }))

import router from '@/router'

const OPS_ROUTES: Array<{ name: string; path: string }> = [
  { name: 'AdminCoverage', path: '/admin/coverage' },
  { name: 'AdminBackfillPlanning', path: '/admin/backfill-planning' },
  { name: 'AdminCapacityGate', path: '/admin/capacity-gate' },
]

beforeEach(async () => {
  authState.isAuthenticated = false
  authState.isAdmin = false
  await router.push('/login')
  await router.isReady()
})

describe('iteration-204 admin ops routes', () => {
  it.each(OPS_ROUTES)('registers $name as an admin-only ops page', async ({ name, path }) => {
    const route = router.getRoutes().find((item) => item.name === name)
    expect(route?.path).toBe(path)
    expect(route?.meta.requiresAdmin).toBe(true)
    expect(route?.meta.title).toBeTruthy()
  })

  it('sends an unauthenticated visitor to login with the requested path', async () => {
    await router.push('/admin/coverage?from=menu')

    expect(router.currentRoute.value.name).toBe('Login')
    expect(router.currentRoute.value.query.redirect).toBe('/admin/coverage?from=menu')
  })

  it.each(OPS_ROUTES)(
    'keeps $name away from non-admins and admits admins',
    async ({ name, path }) => {
      authState.isAuthenticated = true
      await router.push(path)
      expect(router.currentRoute.value.name).toBe('Home')

      authState.isAdmin = true
      await router.push(path)
      expect(router.currentRoute.value.name).toBe(name)
    }
  )

  it('shows one navigation entry per ops page', async () => {
    authState.isAuthenticated = true
    authState.isAdmin = true
    const layoutRouter = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { render: () => h('div') } }],
    })
    await layoutRouter.push('/')
    const wrapper = mount(LayoutView, { global: { plugins: [createPinia(), i18n, layoutRouter] } })

    const entries = wrapper.findAll('.sidebar .el-menu-item').map((item) => item.text())
    expect(entries).toContain('覆盖与新鲜度')
    expect(entries).toContain('补数计划检视')
    expect(entries).toContain('配额门禁')
    wrapper.unmount()
  })
})
