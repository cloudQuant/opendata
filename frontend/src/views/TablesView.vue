<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { tablesApi } from '@/api/tables'
import { getApiErrorMessage } from '@/utils/error'
import type { DataTable } from '@/types'
import { PAGINATION } from '@/config/constants'

const router = useRouter()

const tables = ref<DataTable[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const searchKeyword = ref('')
const currentPage = ref(1)
const pageSize = ref(PAGINATION.DEFAULT_PAGE_SIZE)
const total = ref(0)

let searchDebounceTimer: ReturnType<typeof setTimeout> | null = null

async function loadTables() {
  loading.value = true
  error.value = null
  try {
    const data = await tablesApi.list({
      page: currentPage.value,
      page_size: pageSize.value,
      search: searchKeyword.value || undefined,
    })
    tables.value = data.items ?? []
    total.value = data.total ?? 0
  } catch (e) {
    error.value = e instanceof Error ? e.message : getApiErrorMessage(e)
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
}

function handleViewDetail(table: DataTable) {
  void router.push(`/tables/${table.id}`)
}

function handleSizeChange(size: number) {
  pageSize.value = size
  currentPage.value = 1
  void loadTables()
}

function handleSearch() {
  if (searchDebounceTimer) {
    clearTimeout(searchDebounceTimer)
  }
  searchDebounceTimer = setTimeout(() => {
    currentPage.value = 1
    void loadTables()
  }, 300)
}

onMounted(() => {
  void loadTables()
})
</script>

<template>
  <div class="tables-view">
    <el-card>
      <template #header>
        <div class="header">
          <span>数据表</span>
          <el-tag type="info">
            共 {{ total }} 个表
          </el-tag>
        </div>
      </template>

      <!-- Error Alert -->
      <el-alert
        v-if="error && !loading"
        :title="error"
        type="error"
        :closable="false"
        class="error-alert"
      >
        <el-button type="primary" size="small" @click="loadTables">
          重试
        </el-button>
      </el-alert>

      <div class="content">
        <div class="search-bar">
          <el-input
            v-model="searchKeyword"
            placeholder="搜索表名"
            clearable
            style="max-width: 400px"
            @input="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>

        <el-table
          v-loading="loading"
          :data="tables"
          style="width: 100%"
          stripe
        >
          <el-table-column
            prop="table_name"
            label="表名"
            min-width="200"
          />
          <el-table-column
            prop="row_count"
            label="行数"
            width="120"
          >
            <template #default="{ row }">
              {{ row.row_count?.toLocaleString() || '-' }}
            </template>
          </el-table-column>
          <el-table-column
            prop="created_at"
            label="创建时间"
            width="180"
          >
            <template #default="{ row }">
              {{ new Date(row.created_at).toLocaleString() }}
            </template>
          </el-table-column>
          <el-table-column
            prop="updated_at"
            label="更新时间"
            width="180"
          >
            <template #default="{ row }">
              {{ new Date(row.updated_at).toLocaleString() }}
            </template>
          </el-table-column>
          <el-table-column
            label="操作"
            width="120"
            fixed="right"
          >
            <template #default="{ row }">
              <el-button
                type="primary"
                link
                size="small"
                @click="handleViewDetail(row)"
              >
                查看详情
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="pagination">
          <el-pagination
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :page-sizes="PAGINATION.PAGE_SIZE_OPTIONS"
            :total="total"
            layout="total, sizes, prev, pager, next"
            @size-change="handleSizeChange"
          />
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.tables-view {
  padding: 0;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.search-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.pagination {
  display: flex;
  justify-content: center;
}
</style>
