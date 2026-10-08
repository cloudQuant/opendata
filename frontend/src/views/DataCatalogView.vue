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
  type CatalogCapability,
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
const selectedFunction = ref<{ entry: CatalogEntry; market: string } | null>(null)
const selectedMarket = ref('')
const selectedDomain = ref('')
const selectedSource = ref('')

const UNKNOWN_MARKET = '__unknown_market__'
const UNKNOWN_MARKET_LABEL = '未知市场'
let detailRequestId = 0

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
  const requestId = ++detailRequestId
  detailError.value = null
  detail.value = { entry, page: emptyDataPage(), loading: true }
  try {
    const page = await catalogApi.query(entry.asset_class, entry.domain, {
      page: 1,
      page_size: 20,
    })
    if (requestId === detailRequestId && detail.value?.entry.domain === entry.domain) {
      detail.value.page = page
    }
  } catch (e) {
    if (requestId === detailRequestId) {
      detailError.value = e instanceof Error ? e.message : '查询失败'
      logger.apiError(`/data/${entry.asset_class}/${entry.domain}`, e)
    }
  } finally {
    if (requestId === detailRequestId && detail.value?.entry.domain === entry.domain) {
      detail.value.loading = false
    }
  }
}

function closeDetail() {
  detailRequestId += 1
  detail.value = null
  detailError.value = null
}

function capabilityMarket(capability: CatalogCapability): string {
  return capability.market.trim() || UNKNOWN_MARKET
}

function marketLabel(market: string): string {
  return market === UNKNOWN_MARKET ? UNKNOWN_MARKET_LABEL : market
}

const marketOptions = computed(() => {
  const markets = new Set(catalog.value.markets ?? [])
  const hasUnknown = catalog.value.domains.some((entry) => {
    const capabilities = entry.capabilities ?? []
    return capabilities.length === 0 || capabilities.some((capability) => !capability.market.trim())
  })
  if (hasUnknown) markets.add(UNKNOWN_MARKET)
  return [...markets].sort((left, right) => left.localeCompare(right))
})

const domainOptions = computed(() =>
  [...catalog.value.domains].sort((left, right) => left.domain.localeCompare(right.domain))
)

const sourceOptions = computed(() => {
  const sources = new Set<string>()
  for (const entry of catalog.value.domains) {
    for (const capability of entry.capabilities ?? []) sources.add(capability.source)
  }
  return [...sources].sort((left, right) => left.localeCompare(right))
})

const marketGroups = computed(() => {
  const groups = new Map<string, CatalogEntry[]>()
  for (const entry of catalog.value.domains) {
    if (selectedDomain.value && entry.domain !== selectedDomain.value) continue
    const capabilities = (entry.capabilities ?? []).filter(
      (capability) => !selectedSource.value || capability.source === selectedSource.value
    )
    if (selectedSource.value && !capabilities.length) continue
    const markets = capabilities.length
      ? [...new Set(capabilities.map(capabilityMarket))]
      : [UNKNOWN_MARKET]
    for (const market of markets) {
      if (selectedMarket.value && selectedMarket.value !== market) continue
      const rows = groups.get(market) ?? []
      if (!rows.some((row) => row.domain === entry.domain)) rows.push(entry)
      groups.set(market, rows)
    }
  }
  return [...groups.entries()]
    .sort(([left], [right]) => {
      if (left === UNKNOWN_MARKET) return 1
      if (right === UNKNOWN_MARKET) return -1
      return left.localeCompare(right)
    })
    .map(([market, entries]) => ({ market, entries }))
})

const functionCapabilities = computed(() => {
  if (!selectedFunction.value) return []
  return (selectedFunction.value.entry.capabilities ?? []).filter(
    (capability) =>
      capabilityMarket(capability) === selectedFunction.value?.market &&
      (!selectedSource.value || capability.source === selectedSource.value)
  )
})

function openFunctions(entry: CatalogEntry, market: string) {
  selectedFunction.value = { entry, market }
}

function closeFunctions() {
  selectedFunction.value = null
}

const functionVisible = computed({
  get: () => selectedFunction.value !== null,
  set: (visible: boolean) => {
    if (!visible) closeFunctions()
  },
})

function formatParameters(capability: CatalogCapability): string {
  if (!capability.parameters.length) return '未提供参数架构'
  return capability.parameters
    .map(
      (parameter) => `${parameter.name}: ${parameter.type}${parameter.required ? '（必填）' : ''}`
    )
    .join(' · ')
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
      <div>
        <h2>数据目录</h2>
        <p class="scope-note">
          覆盖、时间范围、新鲜度和质量按合并数据域统计，不按市场拆分；跨市场数据域会出现在多个市场组。
        </p>
      </div>
      <div class="header-actions">
        <a class="function-link" href="/scripts/functions">查看旧版脚本函数列表</a>
        <div class="header-meta">
          <span v-if="catalog.expected_data_date" class="meta-text">
            基准日 {{ catalog.expected_data_date }} · {{ catalog.domains_total }} 域 ·
            {{ catalog.source_legs_total }} 源腿
          </span>
          <el-button :icon="Refresh" aria-label="刷新" circle :loading="loading" @click="load" />
        </div>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="mb-3" />

    <el-form inline class="catalog-filters" aria-label="数据目录筛选">
      <el-form-item label="市场">
        <el-select
          v-model="selectedMarket"
          data-testid="market-filter"
          placeholder="全部市场"
          clearable
        >
          <el-option label="全部市场" value="" />
          <el-option
            v-for="market in marketOptions"
            :key="market"
            :label="marketLabel(market)"
            :value="market"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="数据集 / 域">
        <el-select
          v-model="selectedDomain"
          data-testid="domain-filter"
          placeholder="全部数据集"
          clearable
        >
          <el-option label="全部数据集" value="" />
          <el-option
            v-for="entry in domainOptions"
            :key="entry.domain"
            :label="`${entry.display_name} (${entry.domain})`"
            :value="entry.domain"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="数据源">
        <el-select
          v-model="selectedSource"
          data-testid="source-filter"
          placeholder="全部数据源"
          clearable
        >
          <el-option label="全部数据源" value="" />
          <el-option
            v-for="source in sourceOptions"
            :key="source"
            :label="source"
            :value="source"
          />
        </el-select>
      </el-form-item>
    </el-form>

    <div v-if="loading && !catalog.domains.length" v-loading="true" class="catalog-loading" />
    <el-empty v-else-if="!loading && !error && !marketGroups.length" description="暂无数据域" />
    <section
      v-for="group in marketGroups"
      :key="group.market"
      class="market-group"
      data-testid="market-group"
    >
      <h3 class="market-heading">
        市场：{{ marketLabel(group.market) }}
        <span>{{ group.entries.length }} 个数据集</span>
      </h3>
      <el-table v-loading="loading" :data="group.entries" stripe empty-text="暂无数据域">
        <el-table-column prop="display_name" label="数据集" min-width="150" />
        <el-table-column prop="domain" label="域标识" min-width="140" show-overflow-tooltip />
        <el-table-column prop="asset_class" label="资产类别" width="100" />
        <el-table-column label="覆盖（域总计）" min-width="140">
          <template #default="{ row }">{{ formatCoverage(row) }}</template>
        </el-table-column>
        <el-table-column label="时间范围（域总计）" min-width="180">
          <template #default="{ row }">{{ formatRange(row) }}</template>
        </el-table-column>
        <el-table-column label="新鲜度（域总计）" min-width="190">
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
        <el-table-column label="质量（域总计）" width="120">
          <template #default="{ row }">
            <el-tag :type="qualityOf(row.quality ? row.quality.flag : null).tag" size="small">
              {{ qualityOf(row.quality ? row.quality.flag : null).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :icon="Search" @click="openDetail(row)">预览</el-button>
            <el-button
              size="small"
              data-testid="registered-functions"
              @click="openFunctions(row, group.market)"
            >
              注册函数
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

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

    <el-dialog
      v-model="functionVisible"
      :title="selectedFunction ? `${selectedFunction.entry.display_name} · 注册 Provider 函数` : ''"
      width="86%"
      destroy-on-close
      @closed="closeFunctions"
    >
      <el-table :data="functionCapabilities" border size="small" empty-text="该市场没有注册函数">
        <el-table-column prop="source" label="数据源" width="110" />
        <el-table-column prop="market" label="实际市场" width="100" />
        <el-table-column prop="period" label="周期" width="90" />
        <el-table-column label="验证状态" width="100">
          <template #default="{ row }">{{ row.verified ? '已验证' : '未验证' }}</template>
        </el-table-column>
        <el-table-column label="注册 callable" min-width="250">
          <template #default="{ row }">
            <code>{{ row.callable.module }}.{{ row.callable.name }}</code>
          </template>
        </el-table-column>
        <el-table-column label="仓库查询端点" min-width="300">
          <template #default="{ row }">
            <template v-if="row.endpoint">
              <code>{{ row.endpoint.method }} {{ row.endpoint.path }}</code>
              <div class="endpoint-filters">
                source={{ row.endpoint.query_filters.source }} · period={{
                  row.endpoint.query_filters.period
                }}
              </div>
            </template>
            <span v-else-if="row.model_query_endpoint">
              仓库读取尚未就绪（当前仅提供源模型查询入口）
            </span>
            <span v-else>数据服务尚未接通</span>
          </template>
        </el-table-column>
        <el-table-column label="源模型查询入口" min-width="340">
          <template #default="{ row }">
            <template v-if="row.model_query_endpoint">
              <code>{{ row.model_query_endpoint.method }} {{ row.model_query_endpoint.path }}</code>
              <div class="endpoint-filters">
                原生模型：{{ row.model_query_endpoint.model }}
              </div>
            </template>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="Provider 参数" min-width="260">
          <template #default="{ row }">{{ formatParameters(row) }}</template>
        </el-table-column>
      </el-table>
      <p class="scope-note function-note">
        这里展示注册的 Provider fetcher 元数据；不会触发 Provider 抓取、仓库读取或源模型查询。源模型访问受相应数据源许可约束。
      </p>
      <template #footer>
        <el-button @click="closeFunctions">关闭</el-button>
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
.header-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}
.card-header h2 {
  margin: 0;
}
.scope-note {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.function-link {
  color: var(--el-color-primary);
  font-size: 13px;
  text-decoration: none;
}
.function-link:hover {
  text-decoration: underline;
}
.header-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}
.catalog-filters {
  margin-bottom: 4px;
}
.market-group {
  margin-bottom: 22px;
}
.market-heading {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 18px 0 10px;
  font-size: 16px;
}
.market-heading span {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 400;
}
.catalog-loading {
  min-height: 180px;
}
.endpoint-filters {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.function-note {
  margin-top: 12px;
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
