<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { dataApi, pipelineApi, type FailedShard } from '@/api/data'
import { getApiErrorMessage } from '@/utils/error'
import { logger } from '@/utils/logger'
import type { Execution, ExecutionStats, TaskStatusType } from '@/types'
import { PAGINATION } from '@/config/constants'

const executions = ref<Execution[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const stats = ref<ExecutionStats | null>(null)
const failures = ref<FailedShard[]>([])
const failuresLoading = ref(false)
const retrying = ref(false)
const currentPage = ref(1)
const pageSize = ref(PAGINATION.DEFAULT_PAGE_SIZE)
const total = ref(0)

// Keyed by the backend's own word list (opendata/models/task.py:28-36), and
// typed `Record<TaskStatusType, …>` so the compiler holds the map to it: a
// status added upstream reddens the type plane instead of rendering raw.
const statusMap: Record<
  TaskStatusType,
  { text: string; type: 'info' | 'primary' | 'success' | 'danger' | 'warning' }
> = {
  pending: { text: '等待中', type: 'info' },
  running: { text: '执行中', type: 'primary' },
  completed: { text: '成功', type: 'success' },
  failed: { text: '失败', type: 'danger' },
  timeout: { text: '超时', type: 'warning' },
  cancelled: { text: '已取消', type: 'info' },
}

async function loadExecutions() {
  loading.value = true
  error.value = null
  try {
    const res = await dataApi.listExecutions({
      page: currentPage.value,
      page_size: pageSize.value,
    })
    executions.value = res.items ?? []
    total.value = res.total ?? 0
  } catch (e) {
    error.value = e instanceof Error ? e.message : getApiErrorMessage(e)
  } finally {
    loading.value = false
  }
}

async function loadFailures() {
  failuresLoading.value = true
  try {
    const data = await pipelineApi.failures({ limit: 100 })
    failures.value = data.failures
  } catch (e) {
    logger.apiError('/pipeline/failures', e)
  } finally {
    failuresLoading.value = false
  }
}

async function handleRetryFailed() {
  retrying.value = true
  try {
    const result = await pipelineApi.retryFailed()
    ElMessage.success(`已重置 ${result.reset} 个失败分片，下次运行将自动重试`)
    await loadFailures()
  } catch (e) {
    ElMessage.error(getApiErrorMessage(e))
  } finally {
    retrying.value = false
  }
}

async function loadStats() {
  try {
    stats.value = await dataApi.getStats()
  } catch (e) {
    logger.apiError('/executions/stats', e)
  }
}

function handlePageChange(page: number) {
  currentPage.value = page
  void loadExecutions()
}

function handleSizeChange(size: number) {
  pageSize.value = size
  currentPage.value = 1
  void loadExecutions()
}

function getStatusInfo(status: TaskStatusType) {
  // The word arrives over HTTP: `TaskStatusType` is a claim about it, not a guarantee,
  // and a status this map has not learned yet must render the word rather than take
  // the whole table down on `.text` of undefined.
  return statusMap[status] ?? { text: status, type: 'info' as const }
}

onMounted(async () => {
  await loadFailures()
  await Promise.all([loadExecutions(), loadStats()])
})
</script>

<template>
  <div class="executions-view">
    <!-- Stats Cards -->
    <div
      v-if="stats"
      class="stats-cards"
    >
      <el-card class="stat-card">
        <div class="stat-content">
          <div class="stat-value">
            {{ stats.total_count }}
          </div>
          <div class="stat-label">
            总执行次数
          </div>
        </div>
      </el-card>
      <el-card class="stat-card success">
        <div class="stat-content">
          <div class="stat-value">
            {{ stats.success_count }}
          </div>
          <div class="stat-label">
            成功次数
          </div>
        </div>
      </el-card>
      <el-card class="stat-card danger">
        <div class="stat-content">
          <div class="stat-value">
            {{ stats.failed_count }}
          </div>
          <div class="stat-label">
            失败次数
          </div>
        </div>
      </el-card>
      <el-card class="stat-card warning">
        <div class="stat-content">
          <div class="stat-value">
            {{ stats.success_rate.toFixed(1) }}%
          </div>
          <div class="stat-label">
            成功率
          </div>
        </div>
      </el-card>
    </div>

    <!-- Failed pipeline shards (B3.2 / AC-13 一键重试) -->
    <el-card v-if="failures.length > 0 || failuresLoading" class="failures-card">
      <template #header>
        <div class="failures-header">
          <span>失败分片（管线断点续拉）</span>
          <el-button
            type="danger"
            size="small"
            :loading="retrying"
            @click="handleRetryFailed"
          >
            一键重试
          </el-button>
        </div>
      </template>
      <el-table v-loading="failuresLoading" :data="failures" size="small" max-height="240">
        <el-table-column prop="domain" label="域" width="140" />
        <el-table-column prop="source" label="源" width="90" />
        <el-table-column prop="shard" label="分片" width="70" />
        <el-table-column label="窗口" width="200">
          <template #default="{ row }">
            {{ row.window.start }} .. {{ row.window.end }}
          </template>
        </el-table-column>
        <el-table-column prop="pipeline_id" label="管线" min-width="200" show-overflow-tooltip />
        <el-table-column prop="error" label="错误" min-width="220" show-overflow-tooltip />
      </el-table>
    </el-card>

    <!-- Executions Table -->
    <el-card>
      <template #header>
        <span>执行记录</span>
      </template>

      <!-- Error Alert -->
      <el-alert
        v-if="error && !loading"
        :title="error"
        type="error"
        :closable="false"
        class="error-alert"
      >
        <el-button type="primary" size="small" @click="loadExecutions">
          重试
        </el-button>
      </el-alert>

      <el-table
        v-loading="loading"
        :data="executions"
        style="width: 100%"
        stripe
      >
        <el-table-column
          prop="id"
          label="ID"
          width="80"
        />
        <el-table-column
          prop="script_id"
          label="脚本ID"
          width="100"
        />
        <el-table-column
          label="状态"
          width="100"
        >
          <template #default="{ row }">
            <el-tag
              :type="getStatusInfo(row.status).type"
              size="small"
            >
              {{ getStatusInfo(row.status).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="start_time"
          label="开始时间"
          width="180"
        >
          <template #default="{ row }">
            {{ new Date(row.start_time).toLocaleString() }}
          </template>
        </el-table-column>
        <el-table-column
          prop="duration"
          label="耗时(秒)"
          width="100"
        >
          <template #default="{ row }">
            {{ row.duration ? row.duration.toFixed(2) : '-' }}
          </template>
        </el-table-column>
        <el-table-column
          prop="rows_after"
          label="行数(前/后)"
          width="140"
        >
          <!-- `rows_processed` lived on the download-progress payload only; the
               execution record carries rows_before/rows_after (models/task.py:272-273).
               `??`, not `||`: a run that landed 0 rows is an answer, not a blank. -->
          <template #default="{ row }">
            {{ row.rows_before ?? '-' }} → {{ row.rows_after ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column
          prop="error_message"
          label="错误信息"
          show-overflow-tooltip
        />
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.executions-view {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.stats-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.stat-card {
  text-align: center;
}

.stat-card.success {
  background: #f0f9ff;
  border-color: #10b981;
}

.stat-card.danger {
  background: #fef2f2;
  border-color: #ef4444;
}

.stat-card.warning {
  background: #fffbeb;
  border-color: #f59e0b;
}

.stat-value {
  font-size: 24px;
  font-weight: bold;
  color: #303133;
}

.stat-label {
  font-size: 14px;
  color: #909399;
  margin-top: 8px;
}

.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: center;
}
.failures-card {
  margin-bottom: 16px;
}
.failures-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
