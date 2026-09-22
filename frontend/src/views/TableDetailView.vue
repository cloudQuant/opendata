<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { tablesApi } from '@/api/tables'
import { getApiErrorMessage } from '@/utils/error'
import type { TableDataResponse, TableSchema } from '@/types'

const route = useRoute()
const router = useRouter()

const tableId = ref(route.params.id as string)
const schema = ref<TableSchema | null>(null)
const previewData = ref<TableDataResponse | null>(null)
const loading = ref(false)
const activeTab = ref('schema')

async function loadSchema() {
  loading.value = true
  try {
    schema.value = await tablesApi.getSchema(tableId.value)
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error) || '加载表结构失败')
  } finally {
    loading.value = false
  }
}

async function loadPreview() {
  loading.value = true
  try {
    previewData.value = await tablesApi.getData(tableId.value, 1, 100)
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error) || '加载预览数据失败')
  } finally {
    loading.value = false
  }
}

function handleTabChange(tabName: string | number) {
  activeTab.value = String(tabName)
  if (tabName === 'preview' && !previewData.value) {
    loadPreview()
  }
}

function goBack() {
  router.back()
}

onMounted(() => {
  loadSchema()
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
            <span class="stat-label">大小</span>
            <span class="stat-value">{{ schema.data_size || '-' }}</span>
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
