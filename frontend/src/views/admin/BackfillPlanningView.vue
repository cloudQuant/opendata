<script setup lang="ts">
/**
 * 补数（backfill）计划检视面（iteration-204 / 204-D04-02 第二面）。
 *
 * 这一面没有任何 HTTP 操作可调，页面因此不调用任何接口。已检查的读取面：
 * `opendata/api/*.py` 里没有任何 backfill 路由（`grep -n "backfill" opendata/api/`
 * 只命中 `opendata/api/*.py` 之外的引用），补数计划由
 * `opendata/pipeline/stock_daily_backfill_plan.py` 离线构建，只被
 * `stock_daily_backfill_executor.py` 与 `stock_daily_reconcile.py` 消费；
 * `opendata/api/settings.py` 只对外暴露数据库配置。
 *
 * 处理方式与 PR #182 的「审核运行时未装配」一致：说出状态，不编造数据。
 * 这里也没有新增构建开关——`VITE_REVIEWED_RUNTIME_ENABLED === 'true'` 那种闸门
 * 之所以诚实，是因为背后真的有一个可被装配的进程；补数没有任何可注册的路由，
 * 一个只会改变文案、点下去仍然 404 的开关正是那次改动要消除的故障。
 *
 * 本页能诚实做的只有一件事：校验运维自己填写的提案是否有界（主体、窗口、预算），
 * 并把输入原样回显。所有 dry-run 结果、分片数、blocker 与预算消耗都渲染为
 * 「不可得（无接口）」，绝不给出看起来像计划结果的数字。
 */
import { computed, ref } from 'vue'

const MAX_DATASET_LENGTH = 120
const MAX_INSTRUMENT_LENGTH = 64
/** 代码里的硬上限：`MAX_STOCK_DAILY_WINDOW_DAYS = 3_660`（stock_daily_wire.py:31）。 */
const WINDOW_DAYS_HARD_LIMIT = 3660
/** 代码里的候选分片上限：`_MAX_CANDIDATE_CHUNKS = 25_000`（stock_daily_backfill_plan.py:41）。 */
const CHUNK_BUDGET_HARD_LIMIT = 25_000

const DATASET_PATTERN = /^[a-z0-9][a-z0-9_.:-]*$/
const INSTRUMENT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/

const BACKFILL_NOT_ASSEMBLED_TITLE = '补数计划服务未装配'
const BACKFILL_NOT_ASSEMBLED_MESSAGE =
  '当前部署没有任何补数 HTTP 操作：opendata/api/*.py 下不存在 backfill 路由，计划只在 ' +
  'opendata/pipeline/stock_daily_backfill_plan.py 里离线构建，并由 stock_daily_backfill_executor.py ' +
  '与 stock_daily_reconcile.py 消费。因此本页不调用任何接口，也不显示 dry-run 结果、分片数、' +
  'blocker 或预算消耗；这些一律是「不可得（无接口）」。上线补数面需要后端先注册一个有界计划操作，' +
  '而不是在本页改文案。'

/** 运维自己的输入原样，不是服务端返回的计划。 */
interface Proposal {
  dataset: string
  instrument: string
  start: string
  end: string
  maxWindowDays: number
  chunkBudget: number
  requestedDays: number
}

const dataset = ref('')
const instrument = ref('')
const windowStart = ref('')
const windowEnd = ref('')
const maxWindowDays = ref<number | string>(90)
const chunkBudget = ref<number | string>(500)
const formError = ref<string | null>(null)
const proposal = ref<Proposal | null>(null)
/**
 * 有界性检查只看运维填的东西本身，不预测任何服务端结果；每项要么「成立」要么
 * 「不成立」，没有一项被写成「已批准」。
 */
const admissibility = computed(() => {
  const current = proposal.value
  const hasSubject = current !== null && current.dataset !== '' && current.instrument !== ''
  return [
    {
      label: '主体唯一（dataset × instrument）',
      state: hasSubject ? '成立' : '不成立',
      detail: '两个标识都必填且不含路径字符，否则提案会被拒绝。',
    },
    {
      label: '窗口有界且方向正确',
      state: current !== null && current.requestedDays > 0 ? '成立' : '不成立',
      detail: `要求 start < end，且不超过代码硬上限 ${WINDOW_DAYS_HARD_LIMIT} 天。`,
    },
    {
      label: '预算有界',
      state: current !== null && current.chunkBudget > 0 ? '成立' : '不成立',
      detail: `分片预算必须是 1…${CHUNK_BUDGET_HARD_LIMIT} 之间的整数，超预算的提案不予呈现。`,
    },
  ]
})

/** What the page cannot answer at all — stated, never estimated. */
const unavailableFields = computed(() => [
  'dry-run 结果',
  '候选分片数（candidate_chunks）',
  'blocker 列表（BackfillBlockReason）',
  '实际预算消耗与请求次数',
  '是否可派发执行（execution_authorized）',
])

function parseDate(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const parsed = new Date(`${value}T00:00:00Z`)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function daysBetween(start: string, end: string): number {
  const from = parseDate(start)
  const to = parseDate(end)
  if (from === null || to === null) return Number.NaN
  return Math.round((to.getTime() - from.getTime()) / 86_400_000)
}

function readInteger(value: number | string, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isSafeInteger(parsed) ? parsed : fallback
}

/**
 * Local-only validation: it echoes the operator's own scope and never reaches a
 * transport, because this page declares no endpoint.
 */
function validateProposal(): void {
  formError.value = null
  proposal.value = null

  const datasetId = dataset.value.trim()
  const symbol = instrument.value.trim()
  if (datasetId === '' || symbol === '') {
    formError.value = '必须同时给出 dataset_id 与标的标识，提案才有范围。'
    return
  }
  if (datasetId.length > MAX_DATASET_LENGTH || !DATASET_PATTERN.test(datasetId)) {
    formError.value = `dataset_id 需为不超过 ${MAX_DATASET_LENGTH} 字符的小写标识（可用 . _ : -）。`
    return
  }
  if (symbol.length > MAX_INSTRUMENT_LENGTH || !INSTRUMENT_PATTERN.test(symbol)) {
    formError.value = `标的标识需为不超过 ${MAX_INSTRUMENT_LENGTH} 字符的标识，且不含路径字符。`
    return
  }
  const days = daysBetween(windowStart.value, windowEnd.value)
  if (!Number.isFinite(days)) {
    formError.value = '窗口两端必须是 YYYY-MM-DD 业务日期。'
    return
  }
  if (days <= 0) {
    formError.value = '窗口必须满足 start < end；闭合同日或反向窗口不会被提交。'
    return
  }
  if (days > WINDOW_DAYS_HARD_LIMIT) {
    formError.value = `窗口 ${days} 天超过代码上限 ${WINDOW_DAYS_HARD_LIMIT} 天，提案被拒绝。`
    return
  }
  const windowLimit = readInteger(maxWindowDays.value, 0)
  const budget = readInteger(chunkBudget.value, 0)
  if (windowLimit < 1 || windowLimit > WINDOW_DAYS_HARD_LIMIT) {
    formError.value = `单片窗口上限必须在 1…${WINDOW_DAYS_HARD_LIMIT} 天之间。`
    return
  }
  if (budget < 1 || budget > CHUNK_BUDGET_HARD_LIMIT) {
    formError.value = `分片预算必须在 1…${CHUNK_BUDGET_HARD_LIMIT} 之间；无预算上限的提案不予呈现。`
    return
  }

  proposal.value = {
    dataset: datasetId,
    instrument: symbol,
    start: windowStart.value,
    end: windowEnd.value,
    maxWindowDays: windowLimit,
    chunkBudget: budget,
    requestedDays: days,
  }
}
</script>

<template>
  <section class="backfill-view" aria-labelledby="backfill-title">
    <header class="page-header">
      <div>
        <h1 id="backfill-title">补数计划检视</h1>
        <p>只校验运维提出的有界补数范围；本页不调用接口，也不启动任何补数。</p>
      </div>
    </header>

    <el-alert
      type="warning"
      :closable="false"
      show-icon
      role="alert"
      :title="BACKFILL_NOT_ASSEMBLED_TITLE"
      :description="BACKFILL_NOT_ASSEMBLED_MESSAGE"
      data-testid="backfill-not-assembled"
    />

    <el-card class="plan-card">
      <template #header>
        <h2>提案范围（本地校验）</h2>
      </template>

      <el-form label-position="top" @submit.prevent="validateProposal">
        <el-form-item label="dataset_id">
          <el-input
            v-model="dataset"
            :maxlength="MAX_DATASET_LENGTH"
            aria-label="dataset_id"
            autocomplete="off"
            placeholder="例如 cn-equity-stock-daily"
          />
        </el-form-item>
        <el-form-item label="标的（instrument）">
          <el-input
            v-model="instrument"
            :maxlength="MAX_INSTRUMENT_LENGTH"
            aria-label="标的"
            autocomplete="off"
            placeholder="例如 600000.SH"
          />
        </el-form-item>
        <el-form-item label="窗口起（YYYY-MM-DD）">
          <el-input
            v-model="windowStart"
            maxlength="10"
            aria-label="窗口起"
            autocomplete="off"
            placeholder="2026-01-01"
          />
        </el-form-item>
        <el-form-item label="窗口止（YYYY-MM-DD，不含）">
          <el-input
            v-model="windowEnd"
            maxlength="10"
            aria-label="窗口止"
            autocomplete="off"
            placeholder="2026-02-01"
          />
        </el-form-item>
        <el-form-item label="单片窗口上限（天）">
          <el-input v-model="maxWindowDays" type="number" aria-label="单片窗口上限" />
        </el-form-item>
        <el-form-item label="分片预算（个）">
          <el-input v-model="chunkBudget" type="number" aria-label="分片预算" />
        </el-form-item>

        <p v-if="formError" class="error-text" role="alert">{{ formError }}</p>
        <el-button type="primary" native-type="submit">仅校验提案范围</el-button>
        <p class="hint" data-testid="backfill-no-run-note">
          本页没有"开始补数"按钮：现有 HTTP 面只有 POST
          /api/v1/pipeline/retry-failed，它不带范围也不带预算，
          会重置全部失败分片，因此不在本页暴露。补数计划在代码层也固定
          execution_authorized=false（stock_daily_backfill_plan.py:240
          校验），连离线计划都不声明可执行。
        </p>
      </el-form>
    </el-card>

    <el-card v-if="proposal" class="plan-card">
      <template #header>
        <h2>提案回显（运维输入原样，非服务端结果）</h2>
      </template>
      <dl class="plan-facts">
        <div>
          <dt>主体</dt>
          <dd>{{ proposal.dataset }} × {{ proposal.instrument }}</dd>
        </div>
        <div>
          <dt>窗口</dt>
          <dd>{{ proposal.start }} → {{ proposal.end }}（{{ proposal.requestedDays }} 天）</dd>
        </div>
        <div>
          <dt>单片上限</dt>
          <dd>{{ proposal.maxWindowDays }} 天</dd>
        </div>
        <div>
          <dt>预算</dt>
          <dd>{{ proposal.chunkBudget }} 个分片</dd>
        </div>
      </dl>

      <ul class="checks">
        <li v-for="check in admissibility" :key="check.label" data-testid="backfill-check">
          <span class="check-state">{{ check.state }}</span>
          <span>{{ check.label }}</span>
          <p class="hint">{{ check.detail }}</p>
        </li>
      </ul>

      <h3>计划契约字段（字段名来自 stock_daily_backfill_plan.py；本页无值可填）</h3>
      <ul class="unavailable">
        <li
          v-for="field in unavailableFields"
          :key="field"
          data-testid="backfill-unavailable-field"
        >
          {{ field }}：<strong>不可得（无接口）</strong>
        </li>
      </ul>
      <p class="hint">
        「不可得」是状态本身：没有 dry-run 接口就没有可信的分片数或预算消耗，任何数字都会是编造。
      </p>
    </el-card>
  </section>
</template>

<style scoped>
.backfill-view {
  display: grid;
  gap: 20px;
  max-width: 1000px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.page-header h1,
.plan-card h2 {
  margin: 0;
}

.plan-card h3 {
  margin: 20px 0 8px;
  font-size: 15px;
}

.page-header p,
.hint {
  color: var(--text-color-secondary, #606266);
}

.plan-facts {
  display: grid;
  gap: 8px;
  margin: 0 0 16px;
}

.plan-facts div {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 12px;
}

.plan-facts dt {
  font-weight: 600;
}

.plan-facts dd {
  min-width: 0;
  margin: 0;
}

.checks,
.unavailable {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-left: 20px;
}

.check-state {
  font-weight: 600;
  margin-right: 8px;
}

.error-text {
  color: var(--el-color-danger);
}

@media (max-width: 640px) {
  .page-header {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
