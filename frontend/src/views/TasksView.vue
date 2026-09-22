<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { tasksApi } from '@/api/tasks'
import { scriptsApi } from '@/api/scripts'
import { getApiErrorMessage } from '@/utils/error'
import type { Task, DataScript } from '@/types'

const router = useRouter()

const tasks = ref<Task[]>([])
const scripts = ref<DataScript[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const currentTask = ref<Task | null>(null)
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

// Task form
const taskForm = ref({
  name: '',
  script_id: '',
  schedule_type: 'daily',
  schedule_expression: '0 0 * * *',
  parameters: {} as Record<string, unknown>,
  is_active: true,
})

const scheduleOptions = [
  { label: '每天', value: 'daily', cron: '0 0 * * *' },
  { label: '每周', value: 'weekly', cron: '0 0 * * 1' },
  { label: '每月', value: 'monthly', cron: '0 0 1 * *' },
  { label: '每小时', value: 'hourly', cron: '0 * * * *' },
  { label: '自定义', value: 'custom', cron: '' },
]

async function loadTasks() {
  loading.value = true
  try {
    const data = await tasksApi.list({
      page: currentPage.value,
      page_size: pageSize.value,
    })
    tasks.value = data.items ?? []
    total.value = data.total ?? 0
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

async function loadScripts() {
  try {
    const data = await scriptsApi.list({ page: 1, page_size: 2000 })
    scripts.value = data.items ?? []
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error))
  }
}

function handleCreate() {
  dialogMode.value = 'create'
  currentTask.value = null
  taskForm.value = {
    name: '',
    script_id: '',
    schedule_type: 'daily',
    schedule_expression: '0 0 * * *',
    parameters: {},
    is_active: true,
  }
  dialogVisible.value = true
}

function handleEdit(task: Task) {
  dialogMode.value = 'edit'
  currentTask.value = task
  taskForm.value = {
    name: task.name,
    script_id: task.script_id,
    schedule_type: task.schedule_type,
    schedule_expression: task.schedule_expression,
    parameters: { ...(task.parameters || {}) },
    is_active: task.is_active,
  }
  dialogVisible.value = true
}

async function handleDelete(task: Task) {
  try {
    await ElMessageBox.confirm('确定要删除这个任务吗？', '确认删除', {
      type: 'warning',
    })
    await tasksApi.delete(task.id)
    ElMessage.success('删除成功')
    loadTasks()
  } catch (error: unknown) {
    if (error !== 'cancel') {
      ElMessage.error(getApiErrorMessage(error))
    }
  }
}

async function handleToggle(task: Task) {
  try {
    await tasksApi.update(task.id, { is_active: !task.is_active })
    loadTasks()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error))
  }
}

async function handleSubmit() {
  try {
    if (dialogMode.value === 'create') {
      await tasksApi.create(taskForm.value)
      ElMessage.success('创建成功')
    } else {
      await tasksApi.update(currentTask.value!.id, taskForm.value)
      ElMessage.success('更新成功')
    }
    dialogVisible.value = false
    loadTasks()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error))
  }
}

function handleScheduleChange(value: string) {
  const option = scheduleOptions.find((opt) => opt.value === value)
  if (option && option.cron) {
    taskForm.value.schedule_expression = option.cron
  }
}

function handleViewExecutions(task: Task) {
  router.push(`/executions?task_id=${task.id}`)
}

onMounted(() => {
  loadTasks()
  loadScripts()
})
</script>

<template>
  <div class="tasks-view">
    <el-card>
      <template #header>
        <div class="header">
          <span>定时任务</span>
          <el-button
            type="primary"
            @click="handleCreate"
          >
            创建任务
          </el-button>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="tasks"
        style="width: 100%"
        stripe
      >
        <el-table-column
          prop="name"
          label="任务名称"
          min-width="180"
        />
        <el-table-column
          prop="script_id"
          label="脚本ID"
          width="100"
        />
        <el-table-column
          label="调度类型"
          width="100"
        >
          <template #default="{ row }">
            <el-tag size="small">
              {{ row.schedule_type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="schedule_expression"
          label="Cron 表达式"
          width="150"
        />
        <el-table-column
          label="状态"
          width="80"
        >
          <template #default="{ row }">
            <el-switch
              :model-value="row.is_active"
              @change="handleToggle(row)"
            />
          </template>
        </el-table-column>
        <el-table-column
          prop="next_run_time"
          label="下次执行"
          width="180"
        >
          <template #default="{ row }">
            {{ row.next_execution_at ? new Date(row.next_execution_at).toLocaleString() : '-' }}
          </template>
        </el-table-column>
        <el-table-column
          label="操作"
          width="220"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              type="primary"
              link
              size="small"
              @click="handleEdit(row)"
            >
              编辑
            </el-button>
            <el-button
              link
              size="small"
              @click="handleViewExecutions(row)"
            >
              执行记录
            </el-button>
            <el-button
              type="danger"
              link
              size="small"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          :current-page="currentPage"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="(page: number) => { currentPage = page; loadTasks() }"
        />
      </div>
    </el-card>

    <!-- Create/Edit Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '创建任务' : '编辑任务'"
      width="600px"
    >
      <el-form
        :model="taskForm"
        label-width="100px"
      >
        <el-form-item
          label="任务名称"
          required
        >
          <el-input
            v-model="taskForm.name"
            placeholder="请输入任务名称"
          />
        </el-form-item>

        <el-form-item
          label="数据接口"
          required
        >
          <el-select
            v-model="taskForm.script_id"
            placeholder="请选择数据接口"
            style="width: 100%"
          >
            <el-option
              v-for="script in scripts"
              :key="script.id"
              :label="script.script_name"
              :value="script.script_id"
            />
          </el-select>
        </el-form-item>

        <el-form-item
          label="调度类型"
          required
        >
          <el-select
            v-model="taskForm.schedule_type"
            style="width: 100%"
            @change="handleScheduleChange"
          >
            <el-option
              v-for="opt in scheduleOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item
          label="Cron 表达式"
          required
        >
          <el-input
            v-model="taskForm.schedule_expression"
            placeholder="请输入 Cron 表达式，如: 0 0 * * *"
          />
          <div class="cron-hint">
            格式: 分 时 日 月 周 (例如: 0 0 * * * 表示每天零点)
          </div>
        </el-form-item>

        <el-form-item label="启用">
          <el-switch v-model="taskForm.is_active" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          @click="handleSubmit"
        >
          {{ dialogMode === 'create' ? '创建' : '保存' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tasks-view {
  padding: 0;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: center;
}

.cron-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
</style>
