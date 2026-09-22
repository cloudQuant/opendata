<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  Document,
  CircleCheck,
  CircleClose,
  TrendCharts,
  DataLine,
  Timer,
  Grid,
  Refresh,
} from '@element-plus/icons-vue'
import { dataApi } from '@/api/data'
import { scriptsApi } from '@/api/scripts'
import { StatCard } from '@/components/common'
import { logger } from '@/utils/logger'
import type { ExecutionStats, DataScript } from '@/types'

const stats = ref<ExecutionStats | null>(null)
const recentScripts = ref<DataScript[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

async function loadStats() {
  loading.value = true
  error.value = null
  try {
    const [statsData, scriptsData] = await Promise.all([
      dataApi.getStats(),
      scriptsApi.list({ page: 1, page_size: 5 }),
    ])
    if (statsData) stats.value = statsData
    if (scriptsData?.items) recentScripts.value = scriptsData.items
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载数据失败'
    logger.error('Failed to load stats:', e)
  } finally {
    loading.value = false
  }
}

function handleRetry() {
  void loadStats()
}

onMounted(async () => {
  await loadStats()
})
</script>

<template>
  <div class="home-view">
    <div class="card-header">
      <h2>数据概览</h2>
    </div>

    <!-- Error Alert -->
    <el-alert
      v-if="error && !loading"
      :title="error"
      type="error"
      :closable="false"
      class="error-alert"
    >
      <el-button type="primary" size="small" @click="handleRetry">
        <el-icon class="mr-2"><Refresh /></el-icon>
        重试
      </el-button>
    </el-alert>

    <div class="stats-grid">
      <template v-if="loading">
        <el-card v-for="i in 4" :key="i" class="stat-card-skeleton">
          <el-skeleton :rows="2" animated />
        </el-card>
      </template>
      <template v-else>
        <StatCard
          :value="stats?.total_count ?? 0"
          label="总执行次数"
          :icon="Document"
          type="primary"
        />
        <StatCard
          :value="stats?.success_count ?? 0"
          label="成功次数"
          :icon="CircleCheck"
          type="success"
        />
        <StatCard
          :value="stats?.failed_count ?? 0"
          label="失败次数"
          :icon="CircleClose"
          type="danger"
        />
        <StatCard
          :value="stats?.success_rate ? `${stats.success_rate.toFixed(1)}%` : '0%'"
          label="成功率"
          :icon="TrendCharts"
          type="info"
        />
      </template>
    </div>

    <div class="quick-actions">
      <el-button type="primary" :icon="DataLine" @click="$router.push('/scripts')">
        浏览数据接口
      </el-button>
      <el-button type="success" :icon="Timer" @click="$router.push('/tasks')">
        管理定时任务
      </el-button>
      <el-button type="warning" :icon="Grid" @click="$router.push('/tables')">
        查看数据表
      </el-button>
    </div>

    <el-card class="recent-scripts">
      <template #header>
        <span>最近使用的接口</span>
      </template>
      <el-table :data="loading ? [] : recentScripts" stripe>
        <el-table-column prop="script_name" label="接口名称" min-width="150" />
        <el-table-column prop="category" label="类别" width="120" />
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button
              type="primary"
              link
              size="small"
              @click="$router.push(`/scripts/${row.script_id}`)"
            >
              查看
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && recentScripts.length === 0" description="暂无最近使用的接口" />
    </el-card>
  </div>
</template>

<style scoped>
.home-view {
  padding: 0;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.card-header h2 {
  margin: 0;
  font-size: 18px;
  color: var(--text-color, #303133);
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}

.stat-card-skeleton {
  height: 100px;
}

.quick-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 24px;
}

.quick-actions .el-button {
  flex: 1;
  min-width: 140px;
}

.recent-scripts {
  margin-top: 16px;
}

@media (max-width: 768px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .quick-actions .el-button {
    min-width: 100%;
  }
}
</style>
