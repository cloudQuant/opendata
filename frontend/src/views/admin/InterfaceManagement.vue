<template>
  <div class="interface-management">
    <el-card>
      <template #header>
        <div class="card-header">
          <h2>接口管理（管理员）</h2>
          <el-button
            type="primary"
            :icon="Plus"
            @click="handleCreate"
          >
            添加自定义接口
          </el-button>
        </div>
      </template>

      <!-- Filters -->
      <div class="filters">
        <el-input
          v-model="searchKeyword"
          placeholder="搜索接口名称或描述"
          :prefix-icon="Search"
          clearable
          style="width: 300px"
          @input="handleSearch"
        />
        <el-select
          v-model="filterCategory"
          placeholder="选择类别"
          clearable
          style="width: 200px"
          @change="loadScripts"
        >
          <el-option
            v-for="cat in categories"
            :key="cat"
            :label="cat"
            :value="cat"
          />
        </el-select>
        <el-select
          v-model="filterType"
          placeholder="接口类型"
          clearable
          style="width: 150px"
          @change="loadScripts"
        >
          <el-option
            label="全部"
            value=""
          />
          <el-option
            label="系统接口"
            value="system"
          />
          <el-option
            label="自定义接口"
            value="custom"
          />
        </el-select>
      </div>

      <!-- Scripts Table -->
      <el-table
        v-loading="loading"
        :data="scripts"
        stripe
        style="width: 100%; margin-top: 20px"
      >
        <el-table-column
          prop="script_id"
          label="接口ID"
          width="180"
        />
        <el-table-column
          prop="script_name"
          label="名称"
          width="200"
        />
        <el-table-column
          prop="category"
          label="类别"
          width="120"
        >
          <template #default="{ row }">
            <el-tag :type="getCategoryType(row.category)">
              {{ row.category }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="description"
          label="描述"
          show-overflow-tooltip
        />
        <el-table-column
          label="类型"
          width="100"
        >
          <template #default="{ row }">
            <el-tag
              :type="row.is_custom ? 'success' : 'info'"
              size="small"
            >
              {{ row.is_custom ? '自定义' : '系统' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="is_active"
          label="状态"
          width="80"
        >
          <template #default="{ row }">
            <el-switch
              v-model="row.is_active"
              @change="handleToggleActive(row)"
            />
          </template>
        </el-table-column>
        <el-table-column
          label="操作"
          width="150"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              v-if="row.is_custom"
              type="primary"
              size="small"
              link
              @click="handleEdit(row)"
            >
              编辑
            </el-button>
            <el-button
              v-if="row.is_custom"
              type="danger"
              size="small"
              link
              @click="handleDelete(row)"
            >
              删除
            </el-button>
            <span
              v-if="!row.is_custom"
              class="text-muted"
            >系统接口</span>
          </template>
        </el-table-column>
      </el-table>

      <!-- Pagination -->
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="total"
        layout="total, sizes, prev, pager, next, jumper"
        style="margin-top: 20px; justify-content: flex-end"
        @size-change="loadScripts"
        @current-change="loadScripts"
      />
    </el-card>

    <!-- Create/Edit Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEditMode ? '编辑自定义接口' : '添加自定义接口'"
      width="600px"
      @close="handleDialogClose"
    >
      <el-form
        ref="formRef"
        :model="formData"
        :rules="formRules"
        label-width="120px"
      >
        <el-form-item
          label="接口ID"
          prop="script_id"
        >
          <el-input
            v-model="formData.script_id"
            placeholder="如: custom_stock_data"
            :disabled="isEditMode"
          />
          <span class="form-tip">唯一标识，创建后不可修改</span>
        </el-form-item>

        <el-form-item
          label="接口名称"
          prop="script_name"
        >
          <el-input
            v-model="formData.script_name"
            placeholder="如: 自定义股票数据"
          />
        </el-form-item>

        <el-form-item
          label="类别"
          prop="category"
        >
          <el-select
            v-model="formData.category"
            placeholder="选择类别"
            style="width: 100%"
          >
            <el-option
              v-for="cat in categoryOptions"
              :key="cat.value"
              :label="cat.label"
              :value="cat.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item
          label="子类别"
          prop="sub_category"
        >
          <el-input
            v-model="formData.sub_category"
            placeholder="如: daily, weekly, monthly"
          />
        </el-form-item>

        <el-form-item label="描述">
          <el-input
            v-model="formData.description"
            type="textarea"
            :rows="3"
            placeholder="接口功能描述"
          />
        </el-form-item>

        <el-form-item label="模块路径">
          <el-input
            v-model="formData.module_path"
            placeholder="如: app.data_fetch.scripts.custom"
          />
          <span class="form-tip">Python模块路径</span>
        </el-form-item>

        <el-form-item label="函数名">
          <el-input
            v-model="formData.function_name"
            placeholder="如: main"
          />
          <span class="form-tip">要执行的函数名称</span>
        </el-form-item>

        <el-form-item label="目标表">
          <el-input
            v-model="formData.target_table"
            placeholder="如: custom_stock_data"
          />
          <span class="form-tip">数据存储的表名</span>
        </el-form-item>

        <el-form-item label="预估时长(秒)">
          <el-input-number
            v-model="formData.estimated_duration"
            :min="0"
            :max="3600"
          />
        </el-form-item>

        <el-form-item label="超时(秒)">
          <el-input-number
            v-model="formData.timeout"
            :min="0"
            :max="7200"
          />
        </el-form-item>

        <el-form-item label="参数配置">
          <el-input
            v-model="parametersJson"
            type="textarea"
            :rows="4"
            placeholder="{&quot;symbol&quot;: {&quot;type&quot;: &quot;string&quot;, &quot;required&quot;: true}}"
          />
          <span class="form-tip">JSON格式的参数定义</span>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="submitting"
          @click="handleSubmit"
        >
          {{ isEditMode ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import { scriptApi } from '@/api/scripts'
import type { DataScript } from '@/types'
import { getApiErrorMessage } from '@/utils/error'

// Data
const scripts = ref<DataScript[]>([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)
const searchKeyword = ref('')
const filterCategory = ref('')
const filterType = ref('')
const categories = ref<string[]>([])

// Dialog
const dialogVisible = ref(false)
const isEditMode = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()

// Form data
const formData = reactive({
  script_id: '',
  script_name: '',
  category: '',
  sub_category: '',
  description: '',
  module_path: '',
  function_name: '',
  target_table: '',
  estimated_duration: 60,
  timeout: 300,
  parameters: null as Record<string, any> | null
})

const parametersJson = ref('')

// Category options
const categoryOptions = [
  { value: 'stocks', label: '股票' },
  { value: 'funds', label: '基金' },
  { value: 'futures', label: '期货' },
  { value: 'macro', label: '宏观经济' },
  { value: 'indicators', label: '技术指标' },
  { value: 'custom', label: '自定义' }
]

// Form rules
const formRules: FormRules = {
  script_id: [
    { required: true, message: '请输入接口ID', trigger: 'blur' },
    { pattern: /^[a-z_][a-z0-9_]*$/, message: '只能包含小写字母、数字和下划线，且以字母或下划线开头', trigger: 'blur' }
  ],
  script_name: [
    { required: true, message: '请输入接口名称', trigger: 'blur' }
  ],
  category: [
    { required: true, message: '请选择类别', trigger: 'change' }
  ]
}

// Methods
const loadScripts = async () => {
  loading.value = true
  try {
    const result = await scriptApi.list({
      page: currentPage.value,
      page_size: pageSize.value,
      keyword: searchKeyword.value || undefined,
      category: filterCategory.value || undefined
    }) as any

    // Filter by type if selected
    let items = result.items || []
    if (filterType.value === 'custom') {
      items = items.filter((s: DataScript) => s.is_custom)
    } else if (filterType.value === 'system') {
      items = items.filter((s: DataScript) => !s.is_custom)
    }

    scripts.value = items
    total.value = filterType.value ? items.length : (result.total || 0)
  } catch (error) {
    ElMessage.error('加载接口列表失败')
    console.error(error)
  } finally {
    loading.value = false
  }
}

const loadCategories = async () => {
  try {
    const result = await scriptApi.getCategories() as any
    categories.value = Array.isArray(result) ? result : (result?.data || [])
  } catch (error) {
    console.error('Failed to load categories:', error)
  }
}

const handleSearch = () => {
  currentPage.value = 1
  loadScripts()
}

const handleCreate = () => {
  isEditMode.value = false
  resetForm()
  dialogVisible.value = true
}

const handleEdit = (row: DataScript) => {
  isEditMode.value = true
  Object.assign(formData, {
    script_id: row.script_id,
    script_name: row.script_name,
    category: row.category,
    sub_category: row.sub_category || '',
    description: row.description || '',
    module_path: row.module_path || '',
    function_name: row.function_name || '',
    target_table: row.target_table || '',
    estimated_duration: row.estimated_duration || 60,
    timeout: row.timeout || 300,
    parameters: null
  })
  parametersJson.value = row.parameters ? JSON.stringify(row.parameters, null, 2) : ''
  dialogVisible.value = true
}

const handleDelete = (row: DataScript) => {
  ElMessageBox.confirm(
    `确定要删除自定义接口 "${row.script_name}" 吗？此操作不可恢复。`,
    '删除确认',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    }
  ).then(async () => {
    try {
      await scriptApi.delete(row.script_id)
      ElMessage.success('删除成功')
      loadScripts()
    } catch (error: unknown) {
      ElMessage.error(getApiErrorMessage(error, '删除失败'))
    }
  })
}

const handleToggleActive = async (row: DataScript) => {
  try {
    await scriptApi.toggle(row.script_id)
    ElMessage.success(`接口已${row.is_active ? '启用' : '禁用'}`)
  } catch {
    // Revert on error
    row.is_active = !row.is_active
    ElMessage.error('操作失败')
  }
}

const handleSubmit = async () => {
  if (!formRef.value) return

  await formRef.value.validate(async (valid) => {
    if (!valid) return

    // Parse parameters JSON
    if (parametersJson.value.trim()) {
      try {
        formData.parameters = JSON.parse(parametersJson.value)
      } catch {
        ElMessage.error('参数配置必须是有效的JSON格式')
        return
      }
    } else {
      formData.parameters = null
    }

    submitting.value = true
    try {
      if (isEditMode.value) {
        await scriptApi.update(formData.script_id, {
          script_name: formData.script_name,
          description: formData.description,
          sub_category: formData.sub_category || undefined,
          parameters: formData.parameters,
          estimated_duration: formData.estimated_duration,
          timeout: formData.timeout
        })
        ElMessage.success('更新成功')
      } else {
        await scriptApi.create({
          script_id: formData.script_id,
          script_name: formData.script_name,
          category: formData.category,
          sub_category: formData.sub_category || undefined,
          description: formData.description || undefined,
          module_path: formData.module_path || undefined,
          function_name: formData.function_name || undefined,
          target_table: formData.target_table || undefined,
          estimated_duration: formData.estimated_duration,
          timeout: formData.timeout,
          parameters: formData.parameters || undefined,
          source: 'custom'
        })
        ElMessage.success('创建成功')
      }
      dialogVisible.value = false
      loadScripts()
    } catch (error: unknown) {
      ElMessage.error(getApiErrorMessage(error, '操作失败'))
    } finally {
      submitting.value = false
    }
  })
}

const handleDialogClose = () => {
  resetForm()
}

const resetForm = () => {
  Object.assign(formData, {
    script_id: '',
    script_name: '',
    category: '',
    sub_category: '',
    description: '',
    module_path: '',
    function_name: '',
    target_table: '',
    estimated_duration: 60,
    timeout: 300,
    parameters: null
  })
  parametersJson.value = ''
  formRef.value?.clearValidate()
}

const getCategoryType = (category: string): "success" | "danger" | "primary" | "info" | "warning" | "" => {
  const types: Record<string, "success" | "danger" | "primary" | "info" | "warning" | ""> = {
    stocks: 'primary',
    funds: 'success',
    futures: 'warning',
    macro: 'danger',
    indicators: 'info',
    custom: ''
  }
  return types[category] || ''
}

// Lifecycle
onMounted(() => {
  loadScripts()
  loadCategories()
})
</script>

<style scoped>
.interface-management {
  padding: 20px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.filters {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.form-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  display: block;
  margin-top: 4px;
}

.text-muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
