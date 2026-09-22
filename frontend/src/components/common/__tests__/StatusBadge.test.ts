import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import StatusBadge, { StatusType } from '../StatusBadge.vue'
import { defineComponent, h } from 'vue'
const ElTagStub = defineComponent({
  name: 'ElTagStub',
  props: { type: String, size: String, effect: String },
  setup(_props, { slots }) {
    return () => h('span', { class: 'el-tag' }, slots.default ? slots.default() : null)
  }
})

describe('StatusBadge.vue', () => {
  it('displays mapped text for pending status', async () => {
    const wrapper = mount(StatusBadge, {
      props: { status: 'pending' },
      global: {
        components: { 'el-tag': ElTagStub },
      },
    })
    expect(wrapper.text()).toContain('等待中')
  })

  it('allows overriding display text via text prop', async () => {
    const wrapper = mount(StatusBadge, {
      props: { status: 'unknown', text: 'Custom' },
      global: {
        components: { 'el-tag': ElTagStub },
      },
    })
    expect(wrapper.text()).toContain('Custom')
  })

  it('updates displayed text when status prop changes', async () => {
    const wrapper = mount(StatusBadge, {
      props: { status: 'pending' },
      global: {
        components: { 'el-tag': ElTagStub },
      },
    })
    expect(wrapper.text()).toContain('等待中')
    await wrapper.setProps({ status: 'completed' })
    expect(wrapper.text()).toContain('已完成')
  })
})
