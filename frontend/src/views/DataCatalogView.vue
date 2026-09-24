<script setup lang="ts">
/**
 * 数据目录页（AC-18 / B4.5，设计 §10.1）。
 *
 * 按数据域组织的能力清单：资产类别、来源、已验证标记、最新数据日期、
 * 滞后天数与新鲜度状态；点击某域可下钻预览最近数据行（REST 查询）。
 */
import { ref, computed, onMounted } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { catalogApi, type CatalogEntry, type DataPage } from '@/api/catalog'
import { logger } from '@/utils/logger'

const entries = ref<CatalogEntry[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

// Drill-down state
const detail = ref<{ entry: CatalogEntry; page: DataPage; loading: boolean } | null>(null)
const detailError = ref<string | null>(null)

const FRESHNESS_LABEL: Record<string, { text: string; tag: 'success' | 'warning' | 'danger' | 'info' }> = {
  fresh: { text: '新鲜', tag: 'success' },
  stale: { text: '滞后', tag: 'warning' },
  missing: { text: '缺失', tag: 'danger' },
}

function freshnessOf(entry: CatalogEntry) {
  if (!entry.status) return { text: '未知', tag: 'info' as const }
  return FRESHNESS_LABEL[entry.status] ?? { text: entry.status, tag: 'info' as const }
}

function formatLatest(entry: CatalogEntry): string {
  if (entry.latest === null) return '—'
  const lag = entry.lag_days
  return lag === null ? String(entry.latest) : `${entry.latest}（滞后 ${lag} 天）`
}

async function load() {
  loading.value = true
  error.value = null
  try {
    entries.value = await catalogApi.catalog()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载目录失败'
    logger.error('Failed to load catalog:', e)
  } finally {
    loading.value = false
  }
}

async function openDetail(entry: CatalogEntry) {
  detailError.value = null
  detail.value = { entry, page: { rows: [], columns: [] } as DataPage, loading: true }
  try {
    detail.value.page = await catalogApi.query(entry.asset_class, entry.domain, {
      page: 1,
      page_size: 20,
    })
  } catch (e) {
    detailError.value = e instanceof Error ? e.message : '查询失败'
    logger.error(`Failed to query ${entry.domain}:`, e)
  } finally {
    if (detail.value) detail.value.loading = false
  }
}

function closeDetail() {
  detail.value = null
  detailError.value = null
}

const detailVisible = computed({
  get: () => detail.value !== null,
  set: (visible: boolean) => {
    if (!visible) closeDetail()
  },
})

onMounted(() => {
  // load() catches its own failures; awaiting here would let an
  // unhandled rejection leak into tests and logs.
  void load()
})
</script>

<template>
  <div class="catalog-view">
    <div class="card-header">
      <h2>数据目录</h2>
      <el-button :icon="Refresh" aria-label="刷新" circle :loading="loading" @click="load" />
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb-3" />

    <el-table v-loading="loading" :data="entries" stripe empty-text="暂无数据域">
      <el-table-column prop="display_name" label="数据域" min-width="160" />
      <el-table-column prop="domain" label="域标识" min-width="150" show-overflow-tooltip />
      <el-table-column prop="asset_class" label="资产类别" width="110" />
      <el-table-column prop="source" label="来源" width="110" />
      <el-table-column label="验证" width="90">
        <template #default="{ row }">
          <el-tag :type="row.verified ? 'success' : 'info'" size="small">
            {{ row.verified ? '已验证' : '未验证' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="新鲜度" min-width="200">
        <template #default="{ row }">
          <span :class="freshnessOf(row).tag">
            <el-tag :type="freshnessOf(row).tag" size="small">{{ freshnessOf(row).text }}</el-tag>
            <span class="latest-text">{{ formatLatest(row) }}</span>
          </span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Search" @click="openDetail(row)">预览</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="detailVisible"
      :title="detail ? `${detail.entry.display_name}（${detail.entry.domain}）` : ''"
      width="80%"
      destroy-on-close
      @closed="closeDetail"
    >
      <el-alert
        v-if="detailError"
        :title="detailError"
        type="error"
        show-icon
        :closable="false"
        class="mb-3"
      />
      <el-table
        v-if="detail && !detail.loading"
        :data="detail.page.rows"
        border
        size="small"
        max-height="480"
        empty-text="该域暂无数据"
      >
        <el-table-column
          v-for="column in detail.page.columns.slice(0, 10)"
          :key="column"
          :prop="column"
          :label="column"
          min-width="110"
          show-overflow-tooltip
        />
      </el-table>
      <div v-if="detail && detail.loading" v-loading="true" class="detail-loading" />
      <template #footer>
        <span v-if="detail" class="detail-footer">
          共 {{ detail.page.count }} 行（最近 20 行预览）· 层 {{ detail.entry.layer }} · 源
          {{ detail.entry.source }}
        </span>
        <el-button @click="closeDetail">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.card-header h2 {
  margin: 0;
}
.latest-text {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.detail-loading {
  min-height: 120px;
}
.detail-footer {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-right: auto;
}
.mb-3 {
  margin-bottom: 12px;
}
</style>
