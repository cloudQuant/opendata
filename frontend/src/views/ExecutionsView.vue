<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { dataApi } from '@/api/data'
import { getApiErrorMessage } from '@/utils/error'
import { logger } from '@/utils/logger'
import type { Execution, ExecutionStats, PaginatedResponse } from '@/types'
import { PAGINATION } from '@/config/constants'

const executions = ref<Execution[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const stats = ref<ExecutionStats | null>(null)
const currentPage = ref(1)
const pageSize = ref(PAGINATION.DEFAULT_PAGE_SIZE)
const total = ref(0)

const statusMap: Record<
  string,
  { text: string; type: 'info' | 'primary' | 'success' | 'danger' | 'warning' }
> = {
  pending: { text: '等待中', type: 'info' },
  running: { text: '执行中', type: 'primary' },
  success: { text: '成功', type: 'success' },
  failed: { text: '失败', type: 'danger' },
}

async function loadExecutions() {
  loading.value = true
  error.value = null
  try {
    const data = await dataApi.listExecutions({
      page: currentPage.value,
      page_size: pageSize.value,
    })
    const res = data as PaginatedResponse<Execution>
    executions.value = res.items ?? []
    total.value = res.total ?? 0
  } catch (e) {
    error.value = e instanceof Error ? e.message : getApiErrorMessage(e)
  } finally {
    loading.value = false
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

function getStatusInfo(status: string) {
  return statusMap[status] || { text: status, type: 'info' }
}

async function handleRetry(execution: Execution) {
  try {
    await dataApi.retry(execution.id)
    await loadExecutions()
  } catch (e) {
    ElMessage.error(getApiErrorMessage(e))
  }
}

onMounted(async () => {
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
            {{ stats.success_rate ? (stats.success_rate * 100).toFixed(1) : 0 }}%
          </div>
          <div class="stat-label">
            成功率
          </div>
        </div>
      </el-card>
    </div>

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
          prop="rows_processed"
          label="处理行数"
          width="100"
        >
          <template #default="{ row }">
            {{ row.rows_processed || '-' }}
          </template>
        </el-table-column>
        <el-table-column
          prop="error_message"
          label="错误信息"
          show-overflow-tooltip
        />
        <el-table-column
          label="操作"
          width="100"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'failed'"
              type="primary"
              link
              size="small"
              @click="handleRetry(row)"
            >
              重试
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next"
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
</style>
