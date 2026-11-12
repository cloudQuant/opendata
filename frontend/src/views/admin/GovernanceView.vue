<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { afiDataApi, type GovernanceProvider, type GovernanceEndpoint } from '@/api/afiData'
import { getApiErrorMessage } from '@/utils/error'

const providers = ref<GovernanceProvider[]>([])
const endpoints = ref<GovernanceEndpoint[]>([])
const loading = ref(false)
const activeProvider = ref<string>('')

async function loadGovernance() {
  loading.value = true
  try {
    const [providerData, endpointData] = await Promise.all([
      afiDataApi.listGovernanceProviders(),
      afiDataApi.listGovernanceEndpoints(),
    ])
    providers.value = providerData.items ?? []
    endpoints.value = endpointData.items ?? []
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

function selectProvider(providerId: string) {
  activeProvider.value = activeProvider.value === providerId ? '' : providerId
}

// Clicking a provider row filters the endpoint table; selecting it again clears.
const visibleEndpoints = computed<GovernanceEndpoint[]>(() =>
  activeProvider.value
    ? endpoints.value.filter((e) => e.provider_id === activeProvider.value)
    : endpoints.value
)

onMounted(loadGovernance)
</script>

<template>
  <div class="governance-view">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>数据治理注册表（移植自 AFI）</span>
          <el-tag type="info" size="small">/afi-data/data-governance</el-tag>
        </div>
      </template>

      <el-skeleton :loading="loading" :rows="6" animated>
        <el-table
          :data="providers"
          stripe
          highlight-current-row
          style="width: 100%"
          @row-click="(row: GovernanceProvider) => selectProvider(row.provider_id)"
        >
          <el-table-column prop="provider_id" label="提供商标识" width="160" />
          <el-table-column prop="name" label="名称" width="180" />
          <el-table-column prop="category" label="分类" width="160" />
          <el-table-column prop="auth_type" label="认证类型" width="120" />
          <el-table-column prop="rate_limit" label="速率限制" width="100" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? 'success' : 'info'">
                {{ row.is_active ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-skeleton>
    </el-card>

    <el-card shadow="never" class="endpoints-card">
      <template #header>
        <div class="card-header">
          <span>数据端点（{{ visibleEndpoints.length }}）</span>
          <el-button v-if="activeProvider" size="small" text @click="activeProvider = ''">
            清除筛选
          </el-button>
        </div>
      </template>
      <el-table :data="visibleEndpoints" stripe style="width: 100%">
        <el-table-column prop="endpoint_name" label="端点标识" min-width="180" />
        <el-table-column prop="provider_id" label="提供商" width="140" />
        <el-table-column prop="display_name" label="显示名称" min-width="180" />
        <el-table-column prop="target_table" label="目标表" min-width="160" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'">
              {{ row.is_active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.governance-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.endpoints-card {
  margin-top: 4px;
}
</style>
