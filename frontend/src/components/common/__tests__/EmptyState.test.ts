import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import EmptyState from '../EmptyState.vue'
import { ElButton, ElEmpty, ElIcon } from 'element-plus'

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal() as any
  return {
    ...actual,
    useRouter: vi.fn(),
    useRoute: vi.fn(),
  }
})

describe('EmptyState.vue', () => {
  it('renders with default props', () => {
    const wrapper = mount(EmptyState)

    expect(wrapper.find('.empty-state').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无数据')
  })

  it('renders with custom text', () => {
    const wrapper = mount(EmptyState, {
      props: {
        text: '没有找到数据',
      },
    })

    expect(wrapper.text()).toContain('没有找到数据')
  })

  it('renders with action button', () => {
    const wrapper = mount(EmptyState, {
      props: {
        actionText: '添加数据',
        actionType: 'primary',
      },
    })

    expect(wrapper.findComponent(ElButton).exists()).toBe(true)
    expect(wrapper.findComponent(ElButton).text()).toContain('添加数据')
  })

  it('emits action-click when button clicked', async () => {
    const wrapper = mount(EmptyState, {
      props: {
        actionText: '刷新',
      },
    })

    await wrapper.findComponent(ElButton).trigger('click')
    expect(wrapper.emitted('action-click')).toBeTruthy()
  })

  it('renders with icon', () => {
    const wrapper = mount(EmptyState, {
      props: {
        icon: 'Plus',
      },
    })

    expect(wrapper.findComponent(ElIcon).exists()).toBe(true)
  })
})
