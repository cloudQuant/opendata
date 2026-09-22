import { describe, it, expect } from 'vitest'
import { mount, VueWrapper } from '@vue/test-utils'
import StatCard from '../StatCard.vue'
import { defineComponent, h } from 'vue'

const ElCardStub = defineComponent({
  name: 'ElCardStub',
  props: { class: [String, Array] },
  setup(_props, { slots, attrs }) {
    // Merge base class with incoming class from the parent binding
    const incoming = attrs?.class ?? ''
    const classes = ['stat-card']
    if (incoming) {
      // incoming could be string like 'hoverable' or 'stat-card hoverable'
      if (Array.isArray(incoming)) classes.push(...incoming)
      else classes.push(String(incoming))
    }
    return () => h('div', { class: classes, 'data-testid': 'stat-card' }, slots.default ? slots.default() : null)
  }
})

describe('StatCard.vue', () => {
  it('renders with value and label', () => {
    const wrapper = mount(StatCard, {
      props: {
        value: 123,
        label: 'Views',
        hoverable: true,
      },
      global: {
        components: {
          'el-card': ElCardStub,
        },
      },
    })

    const card = wrapper.find('[data-testid="stat-card"]')
    expect(card.exists()).toBe(true)
    expect(wrapper.find('.stat-value').text()).toBe('123')
    expect(wrapper.find('.stat-label').text()).toBe('Views')
    // hoverable should add the class to the root el-card stub
    expect(card.classes()).toContain('hoverable')
  })
})
