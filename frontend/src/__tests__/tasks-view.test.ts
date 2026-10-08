import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import TasksView from '@/views/TasksView.vue'
import { scriptsApi } from '@/api/scripts'
import { tasksApi } from '@/api/tasks'
import type { DataScript, PaginatedResponse, Task } from '@/types'

const mocks = vi.hoisted(() => ({
  routerPush: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  confirm: vi.fn(),
}))

vi.mock('@/utils/request', () => ({
  default: vi.fn(() => Promise.reject(new Error('unexpected request outside test stub'))),
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return {
    ...actual,
    useRouter: () => ({ push: mocks.routerPush }),
  }
})

vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>('element-plus')
  return {
    ...actual,
    ElMessage: { success: mocks.success, error: mocks.error },
    ElMessageBox: { confirm: mocks.confirm },
  }
})

const SCRIPT: DataScript = {
  id: 901,
  script_id: 'c32_alpha',
  script_name: 'C32SCRIPT_ALPHA 日线',
  category: '股票数据',
  sub_category: null,
  frequency: 'daily',
  description: 'fixture script',
  source: 'ths',
  target_table: 'dwd_stock_daily',
  module_path: 'opendata.data.providers.ths.models.stock_daily',
  function_name: 'fetch',
  parameters: [],
  estimated_duration: 120,
  timeout: 600,
  is_active: true,
  is_custom: false,
  created_at: '2026-09-25T02:00:00Z',
  updated_at: '2026-09-25T02:00:00Z',
}

const SCRIPT_TASK: Task = {
  id: 71,
  name: 'C32TASK_ALPHA',
  description: 'script task fixture',
  user_id: 1,
  task_kind: 'script',
  script_id: SCRIPT.script_id,
  script_name: SCRIPT.script_name,
  schedule_type: 'daily',
  schedule_expression: '0 0 * * *',
  parameters: { symbol: '000001' },
  is_active: true,
  retry_on_failure: true,
  max_retries: 3,
  timeout: 600,
  last_execution_at: '2026-09-25T02:00:00Z',
  next_execution_at: '2026-09-26T00:00:00Z',
  created_at: '2026-09-24T02:00:00Z',
  updated_at: '2026-09-25T02:00:00Z',
}

const PIPELINE_TASK: Task = {
  ...SCRIPT_TASK,
  id: 72,
  name: 'C32TASK_PIPELINE',
  description: 'pipeline task fixture',
  task_kind: 'pipeline',
  script_id: null,
  script_name: null,
  schedule_expression: '0 2 * * *',
  parameters: { domain: 'stock_daily', source: 'ths' },
  is_active: false,
}

const TASK_PAGE: PaginatedResponse<Task> = {
  items: [SCRIPT_TASK, PIPELINE_TASK],
  total: 2,
  page: 1,
  page_size: 20,
}

const SCRIPT_PAGE: PaginatedResponse<DataScript> = {
  items: [SCRIPT],
  total: 1,
  page: 1,
  page_size: 2000,
}

function flat(wrapper: { text(): string }): string {
  return wrapper.text().replace(/\s+/g, ' ').trim()
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
}

function taskRow(wrapper: ReturnType<typeof mount>, name: string) {
  return wrapper.findAll('tbody tr').find((row) => flat(row).includes(name))!
}

async function mountTasks() {
  vi.spyOn(tasksApi, 'list').mockResolvedValue(TASK_PAGE)
  vi.spyOn(scriptsApi, 'list').mockResolvedValue(SCRIPT_PAGE)
  vi.spyOn(tasksApi, 'create').mockResolvedValue(SCRIPT_TASK)
  vi.spyOn(tasksApi, 'update').mockResolvedValue(SCRIPT_TASK)
  vi.spyOn(tasksApi, 'delete').mockResolvedValue(undefined)
  const wrapper = mount(TasksView)
  await flushPromises()
  return wrapper
}

async function openCreateDialog(wrapper: ReturnType<typeof mount>) {
  await button(wrapper, '创建任务')!.trigger('click')
  await flushPromises()
}

function scriptOption(wrapper: ReturnType<typeof mount>) {
  return wrapper
    .findAllComponents({ name: 'ElOption' })
    .find((option) => option.props('label') === SCRIPT.script_name)
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.routerPush.mockResolvedValue(undefined)
  mocks.confirm.mockResolvedValue('confirm')
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

enableAutoUnmount(afterEach)

describe('TasksView', () => {
  it('loads task/script pages and routes the row action with the backend task id', async () => {
    const wrapper = await mountTasks()

    expect(tasksApi.list).toHaveBeenCalledWith({ page: 1, page_size: 20 })
    expect(scriptsApi.list).toHaveBeenCalledWith({ page: 1, page_size: 2000 })
    expect(flat(taskRow(wrapper, 'C32TASK_ALPHA'))).toContain('c32_alpha')
    expect(flat(taskRow(wrapper, 'C32TASK_PIPELINE'))).toContain('stock_daily')

    const pipelineRow = taskRow(wrapper, 'C32TASK_PIPELINE')
    expect(button(pipelineRow as unknown as ReturnType<typeof mount>, '删除')).toBeUndefined()
    expect(button(pipelineRow as unknown as ReturnType<typeof mount>, '执行')).toBeUndefined()
    await button(taskRow(wrapper, 'C32TASK_ALPHA') as unknown as ReturnType<typeof mount>, '执行记录')!.trigger(
      'click'
    )
    expect(mocks.routerPush).toHaveBeenCalledWith('/executions?task_id=71')
  })

  it('keeps pipeline details read-only while script editing uses script_id', async () => {
    const wrapper = await mountTasks()

    await button(taskRow(wrapper, 'C32TASK_PIPELINE') as unknown as ReturnType<typeof mount>, '查看')!.trigger(
      'click'
    )
    await flushPromises()
    expect(flat(wrapper)).toContain('Pipeline 任务为只读展示')
    expect(flat(wrapper)).toContain('stock_daily')
    expect(wrapper.find('.pipeline-details').exists()).toBe(true)
    expect(wrapper.find('.el-dialog .el-form').exists()).toBe(false)
    expect(button(wrapper, '保存')).toBeUndefined()

    await button(wrapper, '关闭')!.trigger('click')
    await button(taskRow(wrapper, 'C32TASK_ALPHA') as unknown as ReturnType<typeof mount>, '编辑')!.trigger(
      'click'
    )
    await flushPromises()
    expect(wrapper.find('.el-dialog .el-form').exists()).toBe(true)
    const option = scriptOption(wrapper)
    expect(option?.props('value')).toBe('c32_alpha')
    expect(option?.props('value')).not.toBe(SCRIPT.id)
    expect(wrapper.findAllComponents({ name: 'ElSelect' })[0].props('modelValue')).toBe('c32_alpha')
  })

  it('creates a script task with the selected DataScript.script_id, not its numeric row id', async () => {
    const wrapper = await mountTasks()
    await openCreateDialog(wrapper)

    const option = scriptOption(wrapper)
    expect(option?.props('value')).toBe('c32_alpha')
    expect(option?.props('value')).not.toBe(901)
    await wrapper
      .findAllComponents({ name: 'ElSelect' })[0]
      .vm.$emit('update:modelValue', option?.props('value'))
    await wrapper.find('input[placeholder="请输入任务名称"]').setValue('C32TASK_CREATED')
    await button(wrapper, '创建')!.trigger('click')
    await flushPromises()

    expect(tasksApi.create).toHaveBeenCalledWith({
      name: 'C32TASK_CREATED',
      script_id: 'c32_alpha',
      schedule_type: 'daily',
      schedule_expression: '0 0 * * *',
      parameters: {},
      is_active: true,
    })
    expect(mocks.success).toHaveBeenCalledWith('创建成功')
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('does not claim success or close the form when task creation fails', async () => {
    vi.spyOn(tasksApi, 'create').mockRejectedValueOnce(new Error('create rejected'))
    const wrapper = await mountTasks()
    await openCreateDialog(wrapper)
    const option = scriptOption(wrapper)
    await wrapper
      .findAllComponents({ name: 'ElSelect' })[0]
      .vm.$emit('update:modelValue', option?.props('value'))
    await wrapper.find('input[placeholder="请输入任务名称"]').setValue('C32TASK_REJECTED')
    await button(wrapper, '创建')!.trigger('click')
    await flushPromises()

    expect(mocks.error).toHaveBeenCalledWith('create rejected')
    expect(mocks.success).not.toHaveBeenCalled()
    expect(wrapper.find('.el-dialog .el-form').exists()).toBe(true)
  })

  it('does not claim success or close the form when task editing fails', async () => {
    vi.spyOn(tasksApi, 'update').mockRejectedValueOnce(new Error('update rejected'))
    const wrapper = await mountTasks()
    await button(taskRow(wrapper, 'C32TASK_ALPHA') as unknown as ReturnType<typeof mount>, '编辑')!.trigger(
      'click'
    )
    await flushPromises()
    await button(wrapper, '保存')!.trigger('click')
    await flushPromises()

    expect(tasksApi.update).toHaveBeenCalledWith(71, {
      name: 'C32TASK_ALPHA',
      script_id: 'c32_alpha',
      schedule_type: 'daily',
      schedule_expression: '0 0 * * *',
      parameters: { symbol: '000001' },
      is_active: true,
    })
    expect(mocks.error).toHaveBeenCalledWith('update rejected')
    expect(mocks.success).not.toHaveBeenCalled()
    expect(wrapper.find('.el-dialog .el-form').exists()).toBe(true)
  })

  it('does not report task deletion success or remove the row after a failed delete', async () => {
    vi.spyOn(tasksApi, 'delete').mockRejectedValueOnce(new Error('delete rejected'))
    const wrapper = await mountTasks()
    await button(taskRow(wrapper, 'C32TASK_ALPHA') as unknown as ReturnType<typeof mount>, '删除')!.trigger(
      'click'
    )
    await flushPromises()

    expect(mocks.confirm).toHaveBeenCalled()
    expect(tasksApi.delete).toHaveBeenCalledWith(71)
    expect(mocks.error).toHaveBeenCalledWith('delete rejected')
    expect(mocks.success).not.toHaveBeenCalled()
    expect(tasksApi.list).toHaveBeenCalledTimes(1)
    expect(flat(wrapper)).toContain('C32TASK_ALPHA')
  })

  it('shows an API error and an explicit empty state', async () => {
    vi.spyOn(tasksApi, 'list').mockRejectedValueOnce(new Error('tasks unavailable'))
    await mountTasks()
    expect(mocks.error).toHaveBeenCalledWith('tasks unavailable')

    vi.restoreAllMocks()
    vi.clearAllMocks()
    vi.spyOn(tasksApi, 'list').mockResolvedValueOnce({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    })
    vi.spyOn(scriptsApi, 'list').mockResolvedValue(SCRIPT_PAGE)
    const emptyWrapper = mount(TasksView)
    await flushPromises()
    expect(emptyWrapper.find('.el-table__empty-text').exists()).toBe(true)
    expect(emptyWrapper.findAll('tbody tr')).toHaveLength(0)
  })
})
