import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { h } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import LayoutView from '@/views/LayoutView.vue'

vi.mock('@/i18n', async () => {
  const { createI18n } = await import('vue-i18n')
  return {
    default: createI18n({
      legacy: false,
      locale: 'zh-CN',
      messages: {
        'zh-CN': {
          nav: {
            home: '首页',
            catalog: '数据目录',
            tasks: '任务',
            executions: '执行记录',
            tables: '数据表',
            interfaceManagement: '接口管理',
            users: '用户管理',
            settings: '设置',
          },
          theme: { toggleLight: '切换亮色', toggleDark: '切换暗色' },
          common: { role: '角色', logout: '退出登录' },
        },
      },
    }),
    setLocale: vi.fn(),
  }
})

describe('LayoutView navigation', () => {
  it('keeps the data catalog as the sole data entry and uses the opendata mark when collapsed', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { render: () => h('div') } },
        { path: '/data', component: { render: () => h('div') } },
        { path: '/scripts/functions', component: { render: () => h('div') } },
      ],
    })
    await router.push('/data')
    await router.isReady()

    const wrapper = mount(LayoutView, {
      global: { plugins: [createPinia(), i18n, router] },
    })

    const navigationText = wrapper
      .findAll('.sidebar .el-menu-item')
      .map((item) => item.text())
      .join(' ')
    expect(navigationText).toContain('数据目录')
    expect(navigationText).not.toContain('数据接口')

    await wrapper.find('.header-left .el-button').trigger('click')
    expect(wrapper.find('.logo').text()).toBe('od')
  })
})
