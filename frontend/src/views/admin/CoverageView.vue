<script setup lang="ts">
/**
 * 覆盖与新鲜度运维面（iteration-204 / 204-D04-02 第一面）。
 *
 * 一行是一个数据域的覆盖判据，判据只由服务端真正返回的令牌推出：
 * `PROVEN` 需要测得的非空时间窗，`NOT_SUPPORTED` 只在服务端自己声明
 * `metadata_only` / `provider_query` 时给出，其余（含读不到、令牌不认识、
 * 时间窗边界缺失）一律 `UNKNOWN`。`UNKNOWN` 永远不会被渲染成"完整"。
 *
 * 标的级（instrument×dataset）证据覆盖在本契约面不可得：`/api/v1` 的目录读取
 * 只给出域级合并表的行数与标的*计数*，证据绑定的窗口由
 * `/api/v2/datasets/{dataset_id}/coverage`（`opendata/api/published_data.py:1092`）
 * 回答，而该前缀不由浏览器声明的 `api_router` 挂载。本页因此把标的级判据写成
 * UNKNOWN 并给出原因，与 PR #182 对未装配能力的处理方式一致：说出状态，不编造数据。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import {
  buildCoverageRows,
  classifyCoverageToken,
  coverageApi,
  instrumentCoverageRow,
  presentationOf,
  VERDICT_PRESENTATION,
  type CoverageRow,
  type CoverageVerdict,
  type DomainFreshness,
} from '@/api/coverage'
import type { CatalogPayload } from '@/api/catalog'

const MAX_DOMAIN_LENGTH = 80

const router = useRouter()
const authStore = useAuthStore()

const catalog = ref<CatalogPayload | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
const verdictFilter = ref<CoverageVerdict | ''>('')
const keyword = ref('')

const freshnessDomain = ref('')
const freshness = ref<DomainFreshness | null>(null)
const freshnessLoading = ref(false)
const freshnessError = ref<string | null>(null)

let catalogController: AbortController | null = null
let freshnessController: AbortController | null = null

const verdictOrder: CoverageVerdict[] = [
  'PROVEN',
  'PARTIAL',
  'SOURCE_LIMITED',
  'NOT_SUPPORTED',
  'UNKNOWN',
]

/** The gap, stated once: two of the five verdicts have no answering operation here. */
const verdictNotes: Record<CoverageVerdict, string> = {
  PROVEN: '本读取面测得的域级窗口（行数、标的数、起止业务日）。',
  PARTIAL: '本契约面没有会返回缺失区间的操作；出现即为该令牌已上线。',
  SOURCE_LIMITED: '本契约面不暴露源保留边界，因此当前不会给出该判据。',
  NOT_SUPPORTED: '服务端自己声明该域无此类覆盖读数（元数据登记或 provider 直供）。',
  UNKNOWN: '读不到、令牌不认识或窗口不完整时的状态，永不显示为完整。',
}

const rows = computed<CoverageRow[]>(() => {
  if (catalog.value === null) return []
  return buildCoverageRows(catalog.value)
})

const filteredRows = computed<CoverageRow[]>(() => {
  const needle = keyword.value.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (verdictFilter.value !== '' && row.verdict !== verdictFilter.value) return false
    if (needle === '') return true
    return (
      row.subject.toLowerCase().includes(needle) ||
      row.key.toLowerCase().includes(needle) ||
      (row.table ?? '').toLowerCase().includes(needle) ||
      row.reason.toLowerCase().includes(needle)
    )
  })
})

const verdictCounts = computed<Record<CoverageVerdict, number>>(() => {
  const counts: Record<CoverageVerdict, number> = {
    PROVEN: 0,
    PARTIAL: 0,
    SOURCE_LIMITED: 0,
    NOT_SUPPORTED: 0,
    UNKNOWN: 0,
  }
  for (const row of rows.value) counts[row.verdict] += 1
  return counts
})

/** Never a fabricated zero-complete claim: an unread catalog is one big UNKNOWN. */
const catalogState = computed<string>(() => {
  if (loading.value) return '正在读取 GET /api/v1/data/catalog…'
  if (loadError.value !== null) return '覆盖读取失败：未用任何其它来源替代，本页不显示推测的覆盖。'
  if (catalog.value === null) return '尚未读取。'
  if (rows.value.length === 0) return '目录返回零个域：没有可判定的覆盖行（UNKNOWN）。'
  return `已读取 ${rows.value.length} 个域的覆盖判据。`
})

const instrumentRow = computed(() =>
  instrumentCoverageRow(
    freshnessDomain.value.trim() === '' ? '（未填写域）' : freshnessDomain.value.trim(),
    '（任一标的）'
  )
)

const freshnessVerdict = computed<{
  label: string
  tag: 'success' | 'warning' | 'info' | 'danger'
} | null>(() => {
  if (freshness.value === null) return null
  return presentationOf(classifyCoverageToken(freshnessToken(freshness.value)))
})

/**
 * Freshness alone says nothing about historical completeness: `fresh`/`stale`
 * only date the newest row, and `missing` says nothing about why history is
 * absent. So this reading yields a verdict only when the server itself sends a
 * coverage token, and otherwise classifies to UNKNOWN rather than to "complete".
 */
function freshnessToken(reading: DomainFreshness): string | null {
  if (reading.error !== null) return null
  const coverageTokens: string[] = ['complete', 'partial', 'source_limited', 'not_applicable']
  return coverageTokens.includes(reading.status) ? reading.status : null
}

function stopRequests(): void {
  catalogController?.abort()
  freshnessController?.abort()
  catalogController = null
  freshnessController = null
}

async function loadCatalog(): Promise<void> {
  if (!authStore.isAuthenticated || !authStore.isAdmin) return
  stopRequests()
  const controller = new AbortController()
  catalogController = controller
  loading.value = true
  loadError.value = null
  try {
    const payload = await coverageApi.catalog()
    if (controller.signal.aborted || catalogController !== controller) return
    catalog.value = payload
  } catch {
    if (controller.signal.aborted || catalogController !== controller) return
    catalog.value = null
    loadError.value = '覆盖目录读取失败（GET /api/v1/data/catalog）。'
  } finally {
    if (catalogController === controller) {
      loading.value = false
      catalogController = null
    }
  }
}

async function readFreshness(): Promise<void> {
  freshnessError.value = null
  freshness.value = null
  const domain = freshnessDomain.value.trim()
  if (domain.length === 0) {
    freshnessError.value = '请先填写要读取新鲜度的域名。'
    return
  }
  if (domain.length > MAX_DOMAIN_LENGTH) {
    freshnessError.value = `域名最长 ${MAX_DOMAIN_LENGTH} 个字符，未发起请求。`
    return
  }
  freshnessController?.abort()
  const controller = new AbortController()
  freshnessController = controller
  freshnessLoading.value = true
  try {
    const reading = await coverageApi.domainFreshness(domain, {}, controller.signal)
    if (freshnessController !== controller) return
    freshness.value = reading
    if (reading.error !== null) freshnessError.value = reading.error
  } finally {
    if (freshnessController === controller) {
      freshnessLoading.value = false
      freshnessController = null
    }
  }
}

function formatFreshness(reading: DomainFreshness | null): string {
  if (reading === null) return '—'
  if (reading.latest === null) return `${reading.status || 'unknown'} · 无最近更新日`
  const lag = reading.lag_days === null ? '' : `（滞后 ${reading.lag_days} 天）`
  return `${reading.status} · ${reading.latest}${lag}`
}

watch(
  () => [authStore.isAuthenticated, authStore.isAdmin],
  ([authenticated, admin]) => {
    if (authenticated && admin) return
    stopRequests()
    void router.replace(authenticated ? '/' : '/login')
  }
)

onMounted(() => {
  if (authStore.isAuthenticated && authStore.isAdmin) void loadCatalog()
})

onBeforeUnmount(() => {
  stopRequests()
})
</script>

<template>
  <section class="coverage-view" aria-labelledby="coverage-title">
    <header class="page-header">
      <div>
        <h1 id="coverage-title">覆盖与新鲜度</h1>
        <p>{{ catalogState }}</p>
      </div>
      <el-button :loading="loading" :disabled="!authStore.isAdmin" @click="loadCatalog">
        重新读取目录
      </el-button>
    </header>

    <el-alert
      v-if="loadError"
      type="warning"
      :closable="false"
      show-icon
      role="alert"
      title="覆盖读取不可用"
      :description="`${loadError} 本页不会用别的来源或样例数据填补这一格。`"
      data-testid="coverage-unavailable"
    />

    <el-card class="legend-card">
      <template #header>
        <h2>判据口径</h2>
      </template>
      <ul class="legend">
        <li v-for="verdict in verdictOrder" :key="verdict" data-testid="verdict-legend">
          <el-tag :type="VERDICT_PRESENTATION[verdict].tag" size="small">
            {{ VERDICT_PRESENTATION[verdict].label }}
          </el-tag>
          <span class="legend-count">{{ verdictCounts[verdict] }}</span>
          <p class="hint">{{ verdictNotes[verdict] }}</p>
        </li>
      </ul>
      <p class="hint" data-testid="coverage-scope-note">
        PROVEN 的范围只是该域合并表在服务端新鲜度口径下测得的时间窗，不声明标的级历史完整性；
        标的级证据覆盖见下方面板。
      </p>
    </el-card>

    <el-card class="filter-card">
      <div class="filter-row">
        <el-input
          v-model="keyword"
          placeholder="按域名、表名或判据原因搜索"
          clearable
          aria-label="覆盖搜索"
        />
        <el-select v-model="verdictFilter" aria-label="按判据过滤" clearable placeholder="全部判据">
          <el-option label="全部判据" value="" />
          <el-option
            v-for="verdict in verdictOrder"
            :key="verdict"
            :label="VERDICT_PRESENTATION[verdict].label"
            :value="verdict"
          />
        </el-select>
      </div>

      <el-table v-if="filteredRows.length > 0" :data="filteredRows" stripe>
        <el-table-column prop="subject" label="数据域" min-width="200" />
        <el-table-column label="判据" width="180">
          <template #default="{ row }">
            <el-tag :type="presentationOf(row.verdict).tag" size="small">
              {{ presentationOf(row.verdict).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间窗" width="200">
          <template #default="{ row }">{{ row.windowText }}</template>
        </el-table-column>
        <el-table-column label="观测事实" width="190">
          <template #default="{ row }">{{ row.factsText }}</template>
        </el-table-column>
        <el-table-column label="判据来源" width="210">
          <template #default="{ row }">
            <code class="path">{{ row.measuredThrough }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="原因" min-width="260" />
      </el-table>
      <p v-else class="hint" data-testid="coverage-no-rows">
        没有满足条件的覆盖行。空列表是"未读取到判据"，不是"覆盖完整"。
      </p>
    </el-card>

    <el-card class="instrument-card">
      <template #header>
        <h2>标的级（instrument×dataset）证据覆盖</h2>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        role="status"
        title="该判据在本契约面不可得，按 UNKNOWN 显示"
        :description="instrumentRow.reason"
        data-testid="instrument-coverage-unknown"
      />
      <dl class="facts">
        <div>
          <dt>主体</dt>
          <dd>{{ instrumentRow.subject }}</dd>
        </div>
        <div>
          <dt>判据</dt>
          <dd>
            <el-tag :type="VERDICT_PRESENTATION[instrumentRow.verdict].tag" size="small">
              {{ VERDICT_PRESENTATION[instrumentRow.verdict].label }}
            </el-tag>
          </dd>
        </div>
        <div>
          <dt>时间窗</dt>
          <dd>{{ instrumentRow.windowText }}</dd>
        </div>
      </dl>
    </el-card>

    <el-card class="freshness-card">
      <template #header>
        <h2>单域新鲜度读取</h2>
      </template>
      <div class="filter-row">
        <el-input
          v-model="freshnessDomain"
          :maxlength="MAX_DOMAIN_LENGTH"
          aria-label="域名"
          placeholder="输入要读取新鲜度的域名，例如 stock_daily"
          autocomplete="off"
        />
        <el-button :loading="freshnessLoading" @click="readFreshness">读取新鲜度</el-button>
      </div>
      <p v-if="freshnessError" class="error-text" role="alert">{{ freshnessError }}</p>
      <p v-else-if="freshness !== null" class="hint" role="status">
        {{ formatFreshness(freshness) }} · 期望业务日 {{ freshness.expectedDataDate ?? '—' }}
        <el-tag v-if="freshnessVerdict" :type="freshnessVerdict.tag" size="small">
          {{ freshnessVerdict.label }}
        </el-tag>
      </p>
      <p v-else-if="!freshnessLoading" class="hint">
        新鲜度只说明最近更新日与滞后，不说明历史是否完整；未读取时本页不下任何覆盖结论。
      </p>
    </el-card>
  </section>
</template>

<style scoped>
.coverage-view {
  display: grid;
  gap: 20px;
  max-width: 1180px;
  margin: 0 auto;
}

.page-header,
.filter-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.page-header h1,
.legend-card h2,
.filter-card h2,
.instrument-card h2,
.freshness-card h2 {
  margin: 0;
}

.page-header p,
.hint,
.legend p {
  color: var(--text-color-secondary, #606266);
  margin: 6px 0 0;
}

.legend {
  display: grid;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.legend-count {
  margin-left: 8px;
  font-variant-numeric: tabular-nums;
}

.filter-row :deep(.el-input) {
  width: 100%;
}

.filter-row :deep(.el-select) {
  width: 240px;
}

.facts {
  display: grid;
  gap: 8px;
  margin: 16px 0 0;
}

.facts div {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 12px;
}

.facts dt {
  font-weight: 600;
}

.facts dd {
  min-width: 0;
  margin: 0;
}

.path,
.facts code {
  overflow-wrap: anywhere;
}

.error-text {
  color: var(--el-color-danger);
}

@media (max-width: 720px) {
  .page-header,
  .filter-row {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
