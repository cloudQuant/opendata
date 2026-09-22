<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { scriptsApi } from '@/api/scripts'
import { dataApi } from '@/api/data'
import { getApiErrorMessage } from '@/utils/error'
import type { DataScript, Parameter } from '@/types'

const route = useRoute()
const router = useRouter()

const script = ref<DataScript | null>(null)
const loading = ref(false)
const downloading = ref(false)
const executionId = ref<number | null>(null)

async function loadScript() {
  loading.value = true
  try {
    const id = route.params.id as string
    script.value = await scriptsApi.getDetail(id)
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error) || '加载接口详情失败')
  } finally {
    loading.value = false
  }
}

async function handleDownload() {
  if (!script.value) return

  downloading.value = true
  try {
    const result = await dataApi.download(script.value.id, {})
    executionId.value = result.execution_id
    ElMessage.success('下载任务已创建')
    await router.push('/executions')
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error) || '创建下载任务失败')
  } finally {
    downloading.value = false
  }
}

function goBack() {
  void router.back()
}

onMounted(() => {
  void loadScript()
})
</script>

<template>
  <div class="script-detail-view">
    <el-page-header
      title="返回"
      @back="goBack"
    >
      <template #content>
        <span v-if="script">{{ script.script_name }}</span>
        <span v-else>加载中...</span>
      </template>
    </el-page-header>

    <div
      v-loading="loading"
      class="content"
    >
      <el-card
        v-if="script"
        class="detail-card"
      >
        <template #title>
          <div class="card-title">
            <span>{{ script.script_name }}</span>
            <el-tag
              size="small"
              type="success"
            >
              {{ script.category }}
            </el-tag>
          </div>
        </template>

        <el-descriptions
          :column="2"
          border
        >
          <el-descriptions-item label="接口名称">
            {{ script.script_name }}
          </el-descriptions-item>
          <el-descriptions-item label="类别">
            <el-tag size="small">
              {{ script.category }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item
            label="模块路径"
            :span="2"
          >
            <code>{{ script.module_path }}</code>
          </el-descriptions-item>
          <el-descriptions-item
            label="函数名"
            :span="2"
          >
            <code>{{ script.function_name }}</code>
          </el-descriptions-item>
          <el-descriptions-item
            label="描述"
            :span="2"
          >
            {{ script.description || '暂无描述' }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- Parameters -->
        <div
          v-if="script.parameters && Array.isArray(script.parameters) && script.parameters.length > 0"
          class="section"
        >
          <h3>参数</h3>
          <el-table
            :data="(script.parameters as Parameter[])"
            style="width: 100%"
          >
            <el-table-column
              prop="name"
              label="参数名"
              width="150"
            />
            <el-table-column
              prop="type"
              label="类型"
              width="100"
            />
            <el-table-column
              label="必填"
              width="80"
            >
              <template #default="{ row }">
                <el-tag
                  :type="row.required ? 'danger' : 'info'"
                  size="small"
                >
                  {{ row.required ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column
              prop="default_value"
              label="默认值"
              width="120"
            />
            <el-table-column
              prop="description"
              label="说明"
              show-overflow-tooltip
            />
          </el-table>
        </div>

        <!-- Actions -->
        <div class="actions">
          <el-button
            type="primary"
            :loading="downloading"
            @click="handleDownload"
          >
            立即下载
          </el-button>
          <el-button @click="router.push('/tasks')">
            创建定时任务
          </el-button>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.script-detail-view {
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

.card-title {
  display: flex;
  align-items: center;
  gap: 12px;
}

.section {
  margin-top: 24px;
}

.section h3 {
  margin: 0 0 16px;
  font-size: 16px;
  font-weight: 500;
  color: #303133;
}

.actions {
  margin-top: 24px;
  display: flex;
  gap: 12px;
}

code {
  background: #f5f7fa;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: 'Monaco', 'Menlo', 'Consolas', monospace;
  font-size: 13px;
  color: #e74c3c;
}
</style>
