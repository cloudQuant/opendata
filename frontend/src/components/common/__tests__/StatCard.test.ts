import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { markRaw } from 'vue'
import { TrendCharts } from '@element-plus/icons-vue'
import StatCard from '../StatCard.vue'

describe('StatCard.vue', () => {
  // The card is Element Plus's own component, and unplugin-vue-components
  // compiles `<el-card>` into a direct import of it — a global component stub
  // under the name `el-card` is therefore never reached. An earlier version of
  // this file asserted against such a stub: it read the marker the test itself
  // injected, so it could not fail for anything the product did wrong. Every
  // assertion below is on the mounted component's real output.
  it('renders value, label and the hoverable class on the card', () => {
    const wrapper = mount(StatCard, {
      props: {
        value: 123,
        label: 'Views',
        hoverable: true,
      },
    })

    const card = wrapper.find('.stat-card')
    expect(card.exists()).toBe(true)
    expect(card.classes()).toContain('hoverable')
    expect(wrapper.find('.stat-value').text()).toBe('123')
    expect(wrapper.find('.stat-label').text()).toBe('Views')
  })

  it('leaves the hover affordance off when hoverable is false', () => {
    const wrapper = mount(StatCard, {
      props: {
        value: 123,
        label: 'Views',
        hoverable: false,
      },
    })

    expect(wrapper.find('.stat-card').classes()).not.toContain('hoverable')
  })

  it('renders an icon block only when an icon is passed', () => {
    const withoutIcon = mount(StatCard, {
      props: { value: 1, label: 'Rows' },
    })
    expect(withoutIcon.find('.stat-icon').exists()).toBe(false)

    const withIcon = mount(StatCard, {
      props: { value: 1, label: 'Rows', icon: markRaw(TrendCharts) },
    })
    expect(withIcon.find('.stat-icon').exists()).toBe(true)
  })
})
