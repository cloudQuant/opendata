<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { scriptsApi } from '@/api/scripts'
import { dataApi } from '@/api/data'
import { interfacesApi, type DataInterfaceSummary } from '@/api/interfaces'
import { getApiErrorMessage } from '@/utils/error'
import type { DataScript, Parameter } from '@/types'

const route = useRoute()
const router = useRouter()

const script = ref<DataScript | null>(null)
const loading = ref(false)
const downloading = ref(false)
const interfaces = ref<DataInterfaceSummary[]>([])
const interfacesLoading = ref(false)
const interfacesLoaded = ref(false)
const interfacesError = ref('')
const selectedInterfaceId = ref<number | null>(null)

const selectedInterface = computed(() =>
  interfaces.value.find((iface) => iface.id === selectedInterfaceId.value && iface.is_active)
)
const canCreateDownload = computed(() =>
  Boolean(
    script.value &&
      interfacesLoaded.value &&
      !interfacesLoading.value &&
      !interfacesError.value &&
      !downloading.value &&
      selectedInterface.value
  )
)

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

async function loadInterfaces() {
  interfacesLoading.value = true
  interfacesLoaded.value = false
  interfacesError.value = ''
  interfaces.value = []
  selectedInterfaceId.value = null

  try {
    interfaces.value = await interfacesApi.listEnabled()
    interfacesLoaded.value = true
  } catch (error) {
    interfacesError.value = getApiErrorMessage(error, '加载可用数据接口失败，请稍后重试')
  } finally {
    interfacesLoading.value = false
  }
}

async function handleDownload() {
  const interfaceId = selectedInterface.value?.id
  if (!script.value || !canCreateDownload.value || interfaceId === undefined) return

  downloading.value = true
  let result
  try {
    result = await dataApi.download(interfaceId, {})
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error) || '创建下载任务失败')
    downloading.value = false
    return
  }

  downloading.value = false
  if (!Number.isSafeInteger(result.execution_id) || result.execution_id <= 0) return

  ElMessage.success('下载任务已创建')
  try {
    await router.push('/executions')
  } catch {
    ElMessage.error('任务已创建，但暂时无法打开执行记录')
  }
}

function goBack() {
  void router.back()
}

onMounted(() => {
  void loadScript()
  void loadInterfaces()
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

        <div class="section download-section">
          <h3>下载数据</h3>
          <p>选择已启用的数据接口以创建下载任务。</p>

          <el-alert
            v-if="interfacesError"
            :title="interfacesError"
            type="error"
            :closable="false"
            show-icon
          />
          <div
            v-else-if="interfacesLoading"
            class="interface-loading"
            role="status"
          >
            正在加载可用数据接口...
          </div>
          <el-alert
            v-else-if="interfacesLoaded && interfaces.length === 0"
            title="当前没有可用的数据接口，无法创建下载任务"
            type="warning"
            :closable="false"
            show-icon
          />

          <el-button
            v-if="interfacesError"
            class="reload-interfaces"
            @click="loadInterfaces"
          >
            重新加载数据接口
          </el-button>

          <el-form-item
            v-if="interfacesLoaded && interfaces.length > 0"
            label="数据接口"
          >
            <el-select
              v-model="selectedInterfaceId"
              data-testid="download-interface-select"
              :disabled="interfacesLoading || downloading"
              placeholder="请选择数据接口"
              style="width: 100%"
            >
              <el-option
                v-for="iface in interfaces"
                :key="iface.id"
                :label="iface.display_name"
                :value="iface.id"
              >
                <div class="interface-option">
                  <span class="interface-name">{{ iface.display_name }}</span>
                  <span class="interface-description">{{ iface.description || '暂无描述' }}</span>
                </div>
              </el-option>
            </el-select>
          </el-form-item>
        </div>

        <!-- Actions -->
        <div class="actions">
          <el-button
            type="primary"
            :loading="downloading"
            :disabled="!canCreateDownload"
            @click="handleDownload"
          >
            创建下载任务
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

.download-section p {
  margin: -6px 0 16px;
  color: #606266;
}

.interface-loading {
  color: #606266;
  margin: 12px 0;
}

.reload-interfaces {
  margin-top: 12px;
}

.interface-option {
  display: flex;
  flex-direction: column;
  line-height: 1.5;
  padding: 4px 0;
}

.interface-name {
  color: #303133;
}

.interface-description {
  overflow: hidden;
  color: #909399;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
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
