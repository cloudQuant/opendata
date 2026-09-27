<script setup lang="ts">
/**
 * 数据目录页（AC-18 / B4.5，设计 §10.1）。
 *
 * 一行是一个数据域：覆盖（行数 / 标的数）、时间范围、新鲜度、各源最近更新与
 * 质量标记；滞后天数是相对 `expected_data_date`（交易日历，A4.7）算的，
 * 所以周末不会把健康的库标红。点击某域可下钻预览最近数据行（REST 查询）。
 */
import { ref, computed, onMounted } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import {
  catalogApi,
  emptyCatalog,
  emptyDataPage,
  type CatalogEntry,
  type CatalogSource,
  type DataPage,
} from '@/api/catalog'
import { logger } from '@/utils/logger'

const catalog = ref(emptyCatalog())
const loading = ref(true)
const error = ref<string | null>(null)

// Drill-down state
const detail = ref<{ entry: CatalogEntry; page: DataPage; loading: boolean } | null>(null)
const detailError = ref<string | null>(null)

const FRESHNESS_LABEL: Record<
  string,
  { text: string; tag: 'success' | 'warning' | 'danger' | 'info' }
> = {
  fresh: { text: '新鲜', tag: 'success' },
  stale: { text: '滞后', tag: 'warning' },
  missing: { text: '缺失', tag: 'danger' },
  unmapped: { text: '未映射', tag: 'info' },
}

const QUALITY_LABEL: Record<string, { text: string; tag: 'success' | 'warning' | 'info' }> = {
  clean: { text: '一致', tag: 'success' },
  flagged: { text: '有差异', tag: 'warning' },
  unmeasured: { text: '未测量', tag: 'info' },
}

function freshnessOf(status: string | null) {
  if (!status) return { text: '未知', tag: 'info' as const }
  return FRESHNESS_LABEL[status] ?? { text: status, tag: 'info' as const }
}

function qualityOf(flag: string | null) {
  if (!flag) return { text: '—', tag: 'info' as const }
  return QUALITY_LABEL[flag] ?? { text: flag, tag: 'info' as const }
}

function formatLatest(entry: CatalogEntry): string {
  if (entry.latest === null) return '—'
  const lag = entry.lag_days
  return lag === null ? String(entry.latest) : `${entry.latest}（滞后 ${lag} 天）`
}

function formatCoverage(entry: CatalogEntry): string {
  if (!entry.coverage) return '—'
  const symbols = entry.coverage.symbols === null ? '—' : `${entry.coverage.symbols} 标的`
  return `${entry.coverage.rows} 行 · ${symbols}`
}

function formatRange(entry: CatalogEntry): string {
  if (!entry.coverage) return '—'
  const { start, end } = entry.coverage
  return start && end ? `${start} ~ ${end}` : '—'
}

/**
 * "ths 2026-09-25 · 已验证 · akshare 滞后 2 天": each leg speaks for itself.
 * Verification belongs to a (domain, source) capability, not to the domain,
 * so it rides on the leg that earned it.
 */
function formatLeg(leg: CatalogSource): string {
  const verified = leg.verified ? '已验证' : '未验证'
  if (leg.status === 'unmapped') return `${leg.source} 未映射 · ${verified}`
  if (leg.latest === null) return `${leg.source} ${freshnessOf(leg.status).text} · ${verified}`
  const lag = leg.lag_days === null ? '' : `（滞后 ${leg.lag_days} 天）`
  return `${leg.source} ${leg.latest}${lag} · ${verified}`
}

async function load() {
  loading.value = true
  error.value = null
  try {
    catalog.value = await catalogApi.catalog()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载目录失败'
    logger.apiError('/data/catalog', e)
  } finally {
    loading.value = false
  }
}

async function openDetail(entry: CatalogEntry) {
  detailError.value = null
  detail.value = { entry, page: emptyDataPage(), loading: true }
  try {
    detail.value.page = await catalogApi.query(entry.asset_class, entry.domain, {
      page: 1,
      page_size: 20,
    })
  } catch (e) {
    detailError.value = e instanceof Error ? e.message : '查询失败'
    logger.apiError(`/data/${entry.asset_class}/${entry.domain}`, e)
  } finally {
    if (detail.value) detail.value.loading = false
  }
}

function closeDetail() {
  detail.value = null
  detailError.value = null
}

const entries = computed(() => catalog.value.domains)

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
      <div class="header-meta">
        <span v-if="catalog.expected_data_date" class="meta-text">
          基准日 {{ catalog.expected_data_date }} · {{ catalog.domains_total }} 域 ·
          {{ catalog.source_legs_total }} 源腿
        </span>
        <el-button :icon="Refresh" aria-label="刷新" circle :loading="loading" @click="load" />
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb-3" />

    <el-table v-loading="loading" :data="entries" stripe empty-text="暂无数据域">
      <el-table-column prop="display_name" label="数据域" min-width="150" />
      <el-table-column prop="domain" label="域标识" min-width="140" show-overflow-tooltip />
      <el-table-column prop="asset_class" label="资产类别" width="100" />
      <el-table-column label="覆盖" min-width="140">
        <template #default="{ row }">{{ formatCoverage(row) }}</template>
      </el-table-column>
      <el-table-column label="时间范围" min-width="180">
        <template #default="{ row }">{{ formatRange(row) }}</template>
      </el-table-column>
      <el-table-column label="新鲜度" min-width="190">
        <template #default="{ row }">
          <el-tag :type="freshnessOf(row.status).tag" size="small">
            {{ freshnessOf(row.status).text }}
          </el-tag>
          <span class="latest-text">{{ formatLatest(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="各源最近更新" min-width="240">
        <template #default="{ row }">
          <el-tag
            v-for="leg in row.sources"
            :key="leg.source"
            :type="freshnessOf(leg.status).tag"
            :title="leg.reason || undefined"
            size="small"
            class="leg-tag"
          >
            {{ formatLeg(leg) }}
          </el-tag>
          <span v-if="!row.sources.length" class="latest-text">无注册源</span>
        </template>
      </el-table-column>
      <el-table-column label="质量" width="100">
        <template #default="{ row }">
          <el-tag :type="qualityOf(row.quality ? row.quality.flag : null).tag" size="small">
            {{ qualityOf(row.quality ? row.quality.flag : null).text }}
          </el-tag>
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
          共 {{ detail.page.count }} 行（最近 20 行预览）· 表 {{ detail.entry.table }}
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
.header-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}
.meta-text {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.latest-text {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.leg-tag {
  margin-right: 6px;
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
