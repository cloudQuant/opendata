<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { settingsApi } from '@/api/settings'
import { getErrorMessage } from '@/utils/error'

interface DbConfig {
  host: string
  port: number
  database: string
  user: string
  password: string
}

const router = useRouter()
const authStore = useAuthStore()

// System info (health endpoint response)
interface HealthInfo {
  status?: string
  version?: string
  [key: string]: unknown
}
const systemInfo = ref<HealthInfo | null>(null)
const loading = ref(false)

// Database configuration forms
const mainDbFormRef = ref<FormInstance>()
const warehouseDbFormRef = ref<FormInstance>()

const editing = ref(false)
const editingWarehouse = ref(false)
const testingMainDb = ref(false)
const testingWarehouseDb = ref(false)
const showSaveWarning = ref(false)

const mainDbConfig = reactive({
  host: 'localhost',
  port: 3306,
  database: '',
  user: '',
  password: ''
})

const warehouseDbConfig = reactive({
  host: 'localhost',
  port: 3306,
  database: '',
  user: '',
  password: ''
})

let originalMainDb: DbConfig | null = null
let originalWarehouseDb: DbConfig | null = null

const mainDbStatus = ref({ connected: false })
const warehouseDbStatus = ref({ connected: false })

// Form rules
const dbFormRules = {
  host: [{ required: true, message: '请输入主机地址', trigger: 'blur' }],
  port: [{ required: true, message: '请输入端口', trigger: 'blur' }],
  database: [{ required: true, message: '请输入数据库名', trigger: 'blur' }],
  user: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

// System info methods
async function loadSystemInfo() {
  loading.value = true
  try {
    const response = await fetch('/health')
    systemInfo.value = await response.json()
  } catch (error) {
    console.error('Failed to load system info:', error)
  } finally {
    loading.value = false
  }
}

function isHealthy() {
  return systemInfo.value?.status === 'healthy'
}

// Database config methods
const loadConfigs = async () => {
  try {
    const [mainRes, warehouseRes] = await Promise.all([
      settingsApi.getDatabaseConfig(),
      settingsApi.getWarehouseConfig()
    ])

    const mainConfig = mainRes.data || mainRes
    const warehouseConfig = warehouseRes.data || warehouseRes

    Object.assign(mainDbConfig, {
      host: mainConfig.host || 'localhost',
      port: mainConfig.port || 3306,
      database: mainConfig.database || '',
      user: mainConfig.user || '',
      password: ''
    })

    Object.assign(warehouseDbConfig, {
      host: warehouseConfig.host || 'localhost',
      port: warehouseConfig.port || 3306,
      database: warehouseConfig.database || '',
      user: warehouseConfig.user || '',
      password: ''
    })

    mainDbStatus.value = { connected: true }
    warehouseDbStatus.value = { connected: true }
  } catch (error: unknown) {
    ElMessage.error('加载配置失败: ' + getErrorMessage(error))
  }
}

const startEdit = () => {
  originalMainDb = { ...mainDbConfig, password: '' }
  mainDbConfig.password = ''
  editing.value = true
}

const cancelEdit = () => {
  if (originalMainDb) {
    Object.assign(mainDbConfig, originalMainDb)
    originalMainDb = null
  }
  editing.value = false
  mainDbFormRef.value?.clearValidate()
}

const startEditWarehouse = () => {
  originalWarehouseDb = { ...warehouseDbConfig, password: '' }
  warehouseDbConfig.password = ''
  editingWarehouse.value = true
}

const cancelEditWarehouse = () => {
  if (originalWarehouseDb) {
    Object.assign(warehouseDbConfig, originalWarehouseDb)
    originalWarehouseDb = null
  }
  editingWarehouse.value = false
  warehouseDbFormRef.value?.clearValidate()
}

const handleTestMainDb = async () => {
  const valid = await mainDbFormRef.value?.validate().catch(() => false)
  if (!valid) return

  testingMainDb.value = true
  try {
    const result = (await settingsApi.testConnection(mainDbConfig)) as {
      success?: boolean
      message?: string
    }
    if (result?.success !== false) {
      ElMessage.success('主数据库连接成功')
      mainDbStatus.value = { connected: true }
    } else {
      ElMessage.error('连接失败: ' + (result?.message || '未知错误'))
      mainDbStatus.value = { connected: false }
    }
  } catch (error: unknown) {
    ElMessage.error('连接测试失败: ' + getErrorMessage(error))
    mainDbStatus.value = { connected: false }
  } finally {
    testingMainDb.value = false
  }
}

const handleTestWarehouseDb = async () => {
  const valid = await warehouseDbFormRef.value?.validate().catch(() => false)
  if (!valid) return

  testingWarehouseDb.value = true
  try {
    const result = (await settingsApi.testWarehouseConnection(warehouseDbConfig)) as {
      success?: boolean
      message?: string
    }
    if (result?.success !== false) {
      ElMessage.success('数据仓库连接成功')
      warehouseDbStatus.value = { connected: true }
    } else {
      ElMessage.error('连接失败: ' + (result?.message || '未知错误'))
      warehouseDbStatus.value = { connected: false }
    }
  } catch (error: unknown) {
    ElMessage.error('连接测试失败: ' + getErrorMessage(error))
    warehouseDbStatus.value = { connected: false }
  } finally {
    testingWarehouseDb.value = false
  }
}

const handleSaveMainDb = () => {
  showSaveWarning.value = true
}

const handleSaveWarehouseDb = () => {
  showSaveWarning.value = true
}

const confirmSave = async () => {
  showSaveWarning.value = false
  ElMessage.warning('配置更新功能暂未实现。请手动更新 .env 文件并重启服务。')

  editing.value = false
  editingWarehouse.value = false
  if (originalMainDb) {
    Object.assign(mainDbConfig, originalMainDb)
    originalMainDb = null
  }
  if (originalWarehouseDb) {
    Object.assign(warehouseDbConfig, originalWarehouseDb)
    originalWarehouseDb = null
  }
}

onMounted(() => {
  if (!authStore.isAdmin) {
    ElMessage.error('权限不足')
    router.push('/')
    return
  }
  loadSystemInfo()
  loadConfigs()
})
</script>

<template>
  <div class="settings-view">
    <el-card>
      <template #header>
        <span>系统设置</span>
      </template>

      <div
        v-if="!authStore.isAdmin"
        class="no-permission"
      >
        <el-empty description="您没有权限访问此页面" />
      </div>

      <div
        v-else
        class="content"
      >
        <!-- System Health -->
        <el-card
          class="section-card"
          shadow="never"
        >
          <template #title>
            <span>系统状态</span>
          </template>

          <el-descriptions
            v-loading="loading"
            :column="2"
            border
          >
            <el-descriptions-item label="状态">
              <el-tag :type="isHealthy() ? 'success' : 'danger'">
                {{ systemInfo?.status || 'unknown' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="版本">
              {{ systemInfo?.version || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="数据库">
              <el-tag :type="systemInfo?.database === 'connected' ? 'success' : 'danger'">
                {{ systemInfo?.database || 'disconnected' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="调度器">
              <el-tag :type="systemInfo?.scheduler === 'running' ? 'success' : 'warning'">
                {{ systemInfo?.scheduler || 'stopped' }}
              </el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <!-- Database Configuration -->
        <el-row
          :gutter="20"
          style="margin-top: 20px"
        >
          <el-col :span="12">
            <el-card
              class="config-card"
              shadow="never"
            >
              <template #title>
                <div class="card-header">
                  <div class="header-title">
                    <span>主数据库配置</span>
                  </div>
                  <el-tag
                    type="primary"
                    size="small"
                  >
                    应用数据库
                  </el-tag>
                </div>
              </template>

              <el-form
                ref="mainDbFormRef"
                :model="mainDbConfig"
                :rules="dbFormRules"
                label-width="100px"
              >
                <el-form-item
                  label="主机"
                  prop="host"
                >
                  <el-input
                    v-model="mainDbConfig.host"
                    :disabled="!editing"
                  />
                </el-form-item>
                <el-form-item
                  label="端口"
                  prop="port"
                >
                  <el-input-number
                    v-model="mainDbConfig.port"
                    :min="1"
                    :max="65535"
                    :disabled="!editing"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item
                  label="数据库"
                  prop="database"
                >
                  <el-input
                    v-model="mainDbConfig.database"
                    :disabled="!editing"
                  />
                </el-form-item>
                <el-form-item
                  label="用户名"
                  prop="user"
                >
                  <el-input
                    v-model="mainDbConfig.user"
                    :disabled="!editing"
                  />
                </el-form-item>
                <el-form-item
                  label="密码"
                  prop="password"
                >
                  <el-input
                    v-model="mainDbConfig.password"
                    type="password"
                    show-password
                    :disabled="!editing"
                  />
                </el-form-item>
                <el-form-item v-if="editing">
                  <el-button
                    :loading="testingMainDb"
                    @click="handleTestMainDb"
                  >
                    测试连接
                  </el-button>
                </el-form-item>
              </el-form>

              <template #footer>
                <el-button
                  v-if="!editing"
                  type="primary"
                  @click="startEdit"
                >
                  编辑
                </el-button>
                <template v-else>
                  <el-button @click="cancelEdit">
                    取消
                  </el-button>
                  <el-button
                    type="primary"
                    @click="handleSaveMainDb"
                  >
                    保存
                  </el-button>
                </template>
              </template>
            </el-card>
          </el-col>

          <el-col :span="12">
            <el-card
              class="config-card"
              shadow="never"
            >
              <template #title>
                <div class="card-header">
                  <div class="header-title">
                    <span>数据仓库配置</span>
                  </div>
                  <el-tag
                    type="success"
                    size="small"
                  >
                    数据仓库
                  </el-tag>
                </div>
              </template>

              <el-form
                ref="warehouseDbFormRef"
                :model="warehouseDbConfig"
                :rules="dbFormRules"
                label-width="100px"
              >
                <el-form-item
                  label="主机"
                  prop="host"
                >
                  <el-input
                    v-model="warehouseDbConfig.host"
                    :disabled="!editingWarehouse"
                  />
                </el-form-item>
                <el-form-item
                  label="端口"
                  prop="port"
                >
                  <el-input-number
                    v-model="warehouseDbConfig.port"
                    :min="1"
                    :max="65535"
                    :disabled="!editingWarehouse"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item
                  label="数据库"
                  prop="database"
                >
                  <el-input
                    v-model="warehouseDbConfig.database"
                    :disabled="!editingWarehouse"
                  />
                </el-form-item>
                <el-form-item
                  label="用户名"
                  prop="user"
                >
                  <el-input
                    v-model="warehouseDbConfig.user"
                    :disabled="!editingWarehouse"
                  />
                </el-form-item>
                <el-form-item
                  label="密码"
                  prop="password"
                >
                  <el-input
                    v-model="warehouseDbConfig.password"
                    type="password"
                    show-password
                    :disabled="!editingWarehouse"
                  />
                </el-form-item>
                <el-form-item v-if="editingWarehouse">
                  <el-button
                    :loading="testingWarehouseDb"
                    @click="handleTestWarehouseDb"
                  >
                    测试连接
                  </el-button>
                </el-form-item>
              </el-form>

              <template #footer>
                <el-button
                  v-if="!editingWarehouse"
                  type="primary"
                  @click="startEditWarehouse"
                >
                  编辑
                </el-button>
                <template v-else>
                  <el-button @click="cancelEditWarehouse">
                    取消
                  </el-button>
                  <el-button
                    type="primary"
                    @click="handleSaveWarehouseDb"
                  >
                    保存
                  </el-button>
                </template>
              </template>
            </el-card>

            <!-- Connection Status -->
            <el-card
              class="status-card"
              shadow="never"
              style="margin-top: 20px"
            >
              <template #title>
                <span>连接状态</span>
              </template>

              <div class="status-list">
                <div class="status-item">
                  <span>主数据库:</span>
                  <el-tag :type="mainDbStatus.connected ? 'success' : 'danger'">
                    {{ mainDbStatus.connected ? '已连接' : '未连接' }}
                  </el-tag>
                </div>
                <div class="status-item">
                  <span>数据仓库:</span>
                  <el-tag :type="warehouseDbStatus.connected ? 'success' : 'danger'">
                    {{ warehouseDbStatus.connected ? '已连接' : '未连接' }}
                  </el-tag>
                </div>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <!-- Save Warning Dialog -->
        <el-dialog
          v-model="showSaveWarning"
          title="保存配置警告"
          width="400px"
        >
          <el-alert
            title="注意"
            type="warning"
            description="保存数据库配置后，服务将重启，当前会话将断开。请确保您已保存所有重要工作。"
            :closable="false"
            show-icon
          />
          <template #footer>
            <el-button @click="showSaveWarning = false">
              取消
            </el-button>
            <el-button
              type="primary"
              @click="confirmSave"
            >
              确认保存
            </el-button>
          </template>
        </el-dialog>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.settings-view {
  padding: 0;
}

.content {
  display: flex;
  flex-direction: column;
}

.section-card {
  margin-bottom: 0;
}

.config-card {
  min-height: 450px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-card {
  min-height: 150px;
}

.status-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.status-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.no-permission {
  padding: 60px 0;
}
</style>
