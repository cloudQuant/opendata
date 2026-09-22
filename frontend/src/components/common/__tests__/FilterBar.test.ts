import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import FilterBar from '../FilterBar.vue'
import { ElInput, ElSelect } from 'element-plus'

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal() as any
  return {
    ...actual,
    useRouter: vi.fn(),
    useRoute: vi.fn(),
  }
})

describe('FilterBar.vue', () => {
  const mockFilters = [
    {
      key: 'status',
      label: '状态',
      placeholder: '请选择状态',
      options: [
        { label: '全部', value: '' },
        { label: '启用', value: 'active' },
        { label: '禁用', value: 'inactive' },
      ],
    },
    {
      key: 'category',
      label: '分类',
      placeholder: '请选择分类',
      options: [
        { label: '全部', value: '' },
        { label: '股票', value: 'stock' },
      ],
    },
  ]

  it('renders with filters', () => {
    const wrapper = mount(FilterBar, {
      props: {
        filters: mockFilters,
      },
    })

    expect(wrapper.find('.filter-bar').exists()).toBe(true)
    expect(wrapper.findAllComponents(ElSelect).length).toBe(2)
  })

  it('renders with search input', () => {
    const wrapper = mount(FilterBar, {
      props: {
        showSearch: true,
        searchPlaceholder: '搜索...',
      },
    })

    expect(wrapper.findComponent(ElInput).exists()).toBe(true)
  })

  it('emits update:searchValue event on search input', async () => {
    const wrapper = mount(FilterBar, {
      props: {
        showSearch: true,
        searchValue: '',
      },
    })

    const input = wrapper.findComponent(ElInput)
    await input.setValue('test keyword')
    expect(wrapper.emitted('update:searchValue')).toBeTruthy()
    expect(wrapper.emitted('update:searchValue')![0][0]).toBe('test keyword')
  })

  it('emits clear event when clear button clicked', async () => {
    const wrapper = mount(FilterBar, {
      props: {
        showSearch: true,
        searchValue: 'test',
        clearable: true,
      },
    })

    const input = wrapper.findComponent(ElInput)
    await input.vm.handleClear()
    expect(wrapper.emitted('clear')).toBeTruthy()
  })

  it('emits filter-change event when filter value changes', async () => {
    const wrapper = mount(FilterBar, {
      props: {
        filters: mockFilters,
      },
    })

    const selects = wrapper.findAllComponents(ElSelect)
    await selects[0].setValue('active')
    expect(wrapper.emitted('filter-change')).toBeTruthy()
  })
})
