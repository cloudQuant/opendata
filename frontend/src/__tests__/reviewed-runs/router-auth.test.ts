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

beforeEach(async () => {
  authState.isAuthenticated = false
  authState.isAdmin = false
  authState.user = { email: 'admin@example.com', role: 'admin' }
  await router.push('/login')
  await router.isReady()
})

describe('reviewed runs admin route', () => {
  it('registers a distinct admin-only route', () => {
    const route = router.getRoutes().find((item) => item.name === 'ReviewedRuns')
    expect(route?.path).toBe('/admin/reviewed-runs')
    expect(route?.meta.requiresAdmin).toBe(true)
  })

  it('redirects unauthenticated visitors to login with their requested path', async () => {
    await router.push('/admin/reviewed-runs?source=menu')

    expect(router.currentRoute.value.name).toBe('Login')
    expect(router.currentRoute.value.query.redirect).toBe('/admin/reviewed-runs?source=menu')
  })

  it('redirects authenticated non-admin users and admits administrators', async () => {
    authState.isAuthenticated = true
    await router.push('/admin/reviewed-runs')
    expect(router.currentRoute.value.name).toBe('Home')

    authState.isAdmin = true
    await router.push('/admin/reviewed-runs')
    expect(router.currentRoute.value.name).toBe('ReviewedRuns')
  })

  it('shows a separate reviewed-runs entry in the admin navigation', async () => {
    authState.isAuthenticated = true
    authState.isAdmin = true
    const layoutRouter = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { render: () => h('div') } }],
    })
    await layoutRouter.push('/')
    const wrapper = mount(LayoutView, {
      global: { plugins: [createPinia(), i18n, layoutRouter] },
    })

    const entries = wrapper.findAll('.sidebar .el-menu-item').map((item) => item.text())
    expect(entries).toContain('审核运行')
    expect(entries).toContain('接口管理')
    wrapper.unmount()
  })
})
