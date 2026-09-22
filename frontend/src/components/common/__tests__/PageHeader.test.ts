import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PageHeader from '../PageHeader.vue'

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal() as any
  return {
    ...actual,
    useRouter: vi.fn(() => ({
      currentRoute: { value: { path: '/test', name: 'Test' } },
      push: vi.fn(),
      replace: vi.fn(),
    })),
    useRoute: vi.fn(),
  }
})

describe('PageHeader.vue', () => {
  it('renders with title only', () => {
    const wrapper = mount(PageHeader, {
      props: {
        title: '页面标题',
      },
    })

    expect(wrapper.text()).toContain('页面标题')
  })

  it('renders with title and subtitle', () => {
    const wrapper = mount(PageHeader, {
      props: {
        title: '主标题',
        subtitle: '副标题描述',
      },
    })

    expect(wrapper.text()).toContain('主标题')
    expect(wrapper.text()).toContain('副标题描述')
  })

  it('renders with bordered style', () => {
    const wrapper = mount(PageHeader, {
      props: {
        title: '标题',
        bordered: true,
      },
    })

    expect(wrapper.find('.page-header.bordered').exists()).toBe(true)
  })

  it('renders actions slot content', () => {
    const wrapper = mount(PageHeader, {
      props: {
        title: '标题',
        actions: true,
      },
      slots: {
        actions: '<button>操作按钮</button>',
      },
    })

    expect(wrapper.text()).toContain('操作按钮')
  })
})
