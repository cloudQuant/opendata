<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { tablesApi } from '@/api/tables'
import { getApiErrorMessage } from '@/utils/error'
import type { TableDataResponse, TableSchema } from '@/types'

const route = useRoute()
const router = useRouter()

const tableId = ref(String(route.params.id ?? ''))
const schema = ref<TableSchema | null>(null)
const previewData = ref<TableDataResponse | null>(null)
const schemaLoading = ref(false)
const previewLoading = ref(false)
const loading = computed(() => schemaLoading.value || previewLoading.value)
const activeTab = ref('schema')
let routeGeneration = 0
let schemaController: AbortController | null = null
let previewController: AbortController | null = null

function isCurrentRoute(tableIdForRequest: string, generation: number): boolean {
  return (
    generation === routeGeneration &&
    tableIdForRequest === tableId.value &&
    tableIdForRequest === String(route.params.id ?? '')
  )
}

async function loadSchema(
  tableIdForRequest: string,
  generation: number,
  controller: AbortController
) {
  schemaLoading.value = true
  try {
    const result = await tablesApi.getSchema(tableIdForRequest, controller.signal)
    if (isCurrentRoute(tableIdForRequest, generation)) {
      schema.value = result
    }
  } catch (error) {
    if (isCurrentRoute(tableIdForRequest, generation) && !controller.signal.aborted) {
      ElMessage.error(getApiErrorMessage(error) || '加载表结构失败')
    }
  } finally {
    if (isCurrentRoute(tableIdForRequest, generation)) {
      schemaLoading.value = false
    }
    if (schemaController === controller) {
      schemaController = null
    }
  }
}

async function loadPreview(
  tableIdForRequest: string,
  generation: number,
  controller: AbortController
) {
  previewLoading.value = true
  try {
    const result = await tablesApi.getData(tableIdForRequest, 1, 100, controller.signal)
    if (isCurrentRoute(tableIdForRequest, generation)) {
      previewData.value = result
    }
  } catch (error) {
    if (isCurrentRoute(tableIdForRequest, generation) && !controller.signal.aborted) {
      ElMessage.error(getApiErrorMessage(error) || '加载预览数据失败')
    }
  } finally {
    if (isCurrentRoute(tableIdForRequest, generation)) {
      previewLoading.value = false
    }
    if (previewController === controller) {
      previewController = null
    }
  }
}

function handleTabChange(tabName: string | number) {
  activeTab.value = String(tabName)
  if (tabName === 'preview' && !previewData.value && !previewLoading.value) {
    const controller = new AbortController()
    previewController = controller
    void loadPreview(tableId.value, routeGeneration, controller)
  }
}

function goBack() {
  router.back()
}

watch(
  () => String(route.params.id ?? ''),
  (nextTableId) => {
    const generation = ++routeGeneration
    schemaController?.abort()
    previewController?.abort()
    schemaController = null
    previewController = null
    tableId.value = nextTableId
    schema.value = null
    previewData.value = null
    activeTab.value = 'schema'
    schemaLoading.value = false
    previewLoading.value = false
    const controller = new AbortController()
    schemaController = controller
    void loadSchema(nextTableId, generation, controller)
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  routeGeneration += 1
  schemaController?.abort()
  previewController?.abort()
  schemaController = null
  previewController = null
})
</script>

<template>
  <div class="table-detail-view">
    <el-page-header
      title="返回"
      @back="goBack"
    >
      <template #content>
        <span>{{ schema?.table_name || '' }}</span>
      </template>
    </el-page-header>

    <div
      v-loading="loading"
      class="content"
    >
      <el-card
        v-if="schema"
        class="detail-card"
      >
        <!-- Stats -->
        <div class="stats-row">
          <div class="stat-item">
            <span class="stat-label">行数</span>
            <span class="stat-value">{{ schema.row_count?.toLocaleString() || 0 }}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">最后更新</span>
            <span class="stat-value">
              {{ schema.last_update_time ? new Date(schema.last_update_time).toLocaleString() : '-' }}
            </span>
          </div>
        </div>

        <!-- Tabs -->
        <el-tabs
          v-model="activeTab"
          @tab-change="handleTabChange"
        >
          <el-tab-pane
            label="表结构"
            name="schema"
          >
            <el-table
              :data="schema.columns"
              style="width: 100%"
            >
              <el-table-column
                prop="name"
                label="列名"
              />
              <el-table-column
                prop="type"
                label="数据类型"
                width="200"
              />
              <el-table-column
                label="可空"
                width="80"
              >
                <template #default="{ row }">
                  <el-tag
                    :type="row.nullable ? 'success' : 'danger'"
                    size="small"
                  >
                    {{ row.nullable ? '是' : '否' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column
                prop="key"
                label="键"
                width="80"
              />
              <el-table-column
                prop="default"
                label="默认值"
                width="120"
              />
            </el-table>
          </el-tab-pane>

          <el-tab-pane
            label="预览数据"
            name="preview"
          >
            <el-table
              :data="previewData?.rows || []"
              style="width: 100%"
              max-height="500"
              stripe
            >
              <el-table-column
                v-for="col in (previewData?.columns || []).slice(0, 10)"
                :key="col"
                :prop="col"
                :label="col"
                min-width="140"
                show-overflow-tooltip
              />
            </el-table>
            <div
              v-if="!previewData?.rows?.length"
              class="empty-hint"
            >
              <el-empty description="暂无数据" />
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.table-detail-view {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.content {
  min-height: 400px;
}

.detail-card {
  margin-top: 20px;
}

.stats-row {
  display: flex;
  gap: 32px;
  margin-bottom: 24px;
}

.stat-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stat-label {
  font-size: 13px;
  color: #909399;
}

.stat-value {
  font-size: 18px;
  font-weight: 500;
  color: #303133;
}

.empty-hint {
  padding: 40px 0;
  text-align: center;
}
</style>
