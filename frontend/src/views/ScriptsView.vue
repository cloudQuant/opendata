<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { scriptsApi } from '@/api/scripts'
import { getApiErrorMessage } from '@/utils/error'
import { logger } from '@/utils/logger'
import type { DataScript } from '@/types'

const router = useRouter()

const scripts = ref<DataScript[]>([])
const categories = ref<string[]>([])
const loading = ref(false)
const searchKeyword = ref('')
const selectedCategory = ref('')
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

let searchDebounceTimer: ReturnType<typeof setTimeout> | null = null

async function loadScripts() {
  loading.value = true
  try {
    const data = await scriptsApi.list({
      page: currentPage.value,
      page_size: pageSize.value,
      category: selectedCategory.value || undefined,
      keyword: searchKeyword.value || undefined,
    })
    scripts.value = data.items ?? []
    total.value = data.total ?? 0
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

async function loadCategories() {
  try {
    categories.value = await scriptsApi.getCategories()
  } catch (error) {
    logger.apiError('/scripts/categories', error)
  }
}

function handleSizeChange(size: number) {
  pageSize.value = size
  currentPage.value = 1
  void loadScripts()
}

function handleViewDetail(script: DataScript) {
  void router.push(`/scripts/${script.script_id}`)
}

function handleCategoryChange(category: string | number | boolean | undefined) {
  selectedCategory.value = String(category ?? '')
  currentPage.value = 1
  void loadScripts()
}

function handleSearch() {
  if (searchDebounceTimer) {
    clearTimeout(searchDebounceTimer)
  }
  searchDebounceTimer = setTimeout(() => {
    currentPage.value = 1
    void loadScripts()
  }, 300)
}

onMounted(() => {
  void loadCategories()
  void loadScripts()
})
</script>

<template>
  <div class="scripts-view">
    <el-card>
      <template #header>
        <div class="header">
          <span>数据接口</span>
          <el-tag type="info">
            共 {{ total }} 个接口
          </el-tag>
        </div>
      </template>

      <div class="content">
        <!-- Category Filter -->
        <div class="category-filter">
          <el-radio-group
            v-model="selectedCategory"
            @change="handleCategoryChange"
          >
            <el-radio-button value="">
              全部
            </el-radio-button>
            <el-radio-button
              v-for="category in categories"
              :key="category"
              :value="category"
            >
              {{ category }}
            </el-radio-button>
          </el-radio-group>
        </div>

        <!-- Search -->
        <div class="search-bar">
          <el-input
            v-model="searchKeyword"
            placeholder="搜索接口名称或描述"
            clearable
            @input="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>

        <!-- Script List -->
        <el-table
          v-loading="loading"
          :data="scripts"
          style="width: 100%"
          stripe
        >
          <el-table-column
            prop="script_name"
            label="接口名称"
            min-width="200"
          />
          <el-table-column
            prop="description"
            label="描述"
            show-overflow-tooltip
          />
          <el-table-column
            prop="category"
            label="类别"
            width="120"
          >
            <template #default="{ row }">
              <el-tag size="small">
                {{ row.category }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            label="操作"
            width="150"
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

        <!-- Pagination -->
        <div class="pagination">
          <el-pagination
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="total"
            layout="total, sizes, prev, pager, next, jumper"
            @size-change="handleSizeChange"
          />
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.scripts-view {
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

.category-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.search-bar {
  max-width: 400px;
}

.pagination {
  display: flex;
  justify-content: center;
}
</style>
