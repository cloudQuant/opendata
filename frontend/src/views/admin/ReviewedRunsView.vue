<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useReviewedRunsStore } from '@/stores/reviewedRuns'
import type { ReviewedJob, ReviewedRunStatus } from '@/api/reviewedRuns'

const MAX_JSON_LENGTH = 8192
const POLL_INTERVAL_MS = 2000
const MAX_POLL_ATTEMPTS = 120
const RUN_KEY_PATTERN = /^[0-9a-f]{64}$/

const router = useRouter()
const authStore = useAuthStore()
const store = useReviewedRunsStore()

const selectedJobKey = ref('')
const queryText = ref('{}')
const partitionText = ref('{}')
const runKeyText = ref('')
const formError = ref<string | null>(null)
const pollNotice = ref<string | null>(null)
const cancelNotice = ref<string | null>(null)

let jobsController: AbortController | null = null
let submissionController: AbortController | null = null
let lookupController: AbortController | null = null
let cancellationController: AbortController | null = null
let pollController: AbortController | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollEpoch = 0
let pollAttempts = 0

function jobOptionValue(job: ReviewedJob): string {
  return `${typeof job.job_id}:${job.job_id}:${typeof job.version}:${job.version}`
}

const selectedJob = computed(
  () => store.runnableJobs.find((job) => jobOptionValue(job) === selectedJobKey.value) ?? null
)
const isActiveRun = computed(
  () => store.currentRun !== null && store.currentRun.status in activeRunStatus
)

const activeRunStatus: Record<ReviewedRunStatus, boolean> = {
  queued: true,
  running: true,
  succeeded: false,
  failed: false,
  cancelled: false,
}

function stopPolling(): void {
  pollEpoch += 1
  if (pollTimer !== null) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
  pollController?.abort()
  pollController = null
}

function stopAllRequests(): void {
  jobsController?.abort()
  submissionController?.abort()
  lookupController?.abort()
  cancellationController?.abort()
  jobsController = null
  submissionController = null
  lookupController = null
  cancellationController = null
  stopPolling()
}

function scheduleNextPoll(runKey: string, epoch: number): void {
  if (epoch !== pollEpoch || pollTimer !== null) return
  if (pollAttempts >= MAX_POLL_ATTEMPTS) {
    pollNotice.value = '已达到本页轮询上限。可手动刷新状态继续查看。'
    return
  }

  pollTimer = setTimeout(() => {
    pollTimer = null
    void pollRunOnce(runKey, epoch)
  }, POLL_INTERVAL_MS)
}

async function pollRunOnce(runKey: string, epoch: number): Promise<void> {
  if (epoch !== pollEpoch || store.activeRunKey !== runKey) return

  pollAttempts += 1
  const controller = new AbortController()
  pollController = controller
  const run = await store.fetchRun(runKey, controller.signal)
  if (pollController === controller) pollController = null
  if (epoch !== pollEpoch || store.activeRunKey !== runKey) return
  if (run && store.isTerminal(run.status)) return
  scheduleNextPoll(runKey, epoch)
}

function beginPolling(runKey: string): void {
  stopPolling()
  pollAttempts = 0
  pollNotice.value = null
  const epoch = pollEpoch
  scheduleNextPoll(runKey, epoch)
}

function parseJsonObject(text: string, label: string): Record<string, unknown> | null {
  if (text.length > MAX_JSON_LENGTH) {
    formError.value = `${label}不能超过 ${MAX_JSON_LENGTH} 个字符。`
    return null
  }
  try {
    const parsed: unknown = JSON.parse(text)
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      formError.value = `${label}必须是 JSON 对象，例如 {}。`
      return null
    }
    return parsed as Record<string, unknown>
  } catch {
    formError.value = `${label}不是有效 JSON，请检查逗号、引号和括号。`
    return null
  }
}

function handleJobChange(): void {
  submissionController?.abort()
  lookupController?.abort()
  cancellationController?.abort()
  submissionController = null
  lookupController = null
  cancellationController = null
  stopPolling()
  store.invalidateRunContext()
  store.clearIntent()
  pollNotice.value = null
  cancelNotice.value = null
}

function handleIntentInput(): void {
  store.clearIntent()
  formError.value = null
  if (store.submitting) {
    submissionController?.abort()
    submissionController = null
  }
}

async function submitRun(): Promise<void> {
  formError.value = null
  cancelNotice.value = null
  if (!selectedJob.value) {
    formError.value = '请选择已审核且启用的任务。'
    return
  }
  const query = parseJsonObject(queryText.value, 'query')
  if (!query) return
  const partition = parseJsonObject(partitionText.value, 'partition')
  if (!partition) return

  submissionController?.abort()
  const controller = new AbortController()
  submissionController = controller
  const accepted = await store.startRun(
    {
      jobId: selectedJob.value.job_id,
      version: selectedJob.value.version,
      query,
      partition,
    },
    controller.signal
  )
  if (submissionController === controller) submissionController = null
  if (!accepted || controller.signal.aborted) return
  runKeyText.value = accepted.run_key
  beginPolling(accepted.run_key)
}

async function trackRunKey(): Promise<void> {
  formError.value = null
  cancelNotice.value = null
  const runKey = runKeyText.value.trim()
  if (!RUN_KEY_PATTERN.test(runKey)) {
    formError.value = '运行键必须是 64 位小写十六进制字符串。'
    return
  }

  stopPolling()
  submissionController?.abort()
  submissionController = null
  cancellationController?.abort()
  cancellationController = null
  lookupController?.abort()
  if (!store.selectRunKey(runKey)) {
    formError.value = store.runError
    return
  }
  pollNotice.value = null
  const controller = new AbortController()
  lookupController = controller
  const run = await store.fetchRun(runKey, controller.signal)
  if (lookupController === controller) lookupController = null
  if (controller.signal.aborted || store.activeRunKey !== runKey) return
  if (run && activeRunStatus[run.status]) beginPolling(runKey)
}

async function refreshRun(): Promise<void> {
  const runKey = store.activeRunKey
  if (!runKey) return
  stopPolling()
  lookupController?.abort()
  const controller = new AbortController()
  lookupController = controller
  const run = await store.fetchRun(runKey, controller.signal)
  if (lookupController === controller) lookupController = null
  if (controller.signal.aborted || store.activeRunKey !== runKey) return
  if (run && activeRunStatus[run.status]) beginPolling(runKey)
}

async function cancelCurrentRun(): Promise<void> {
  stopPolling()
  cancellationController?.abort()
  const controller = new AbortController()
  cancellationController = controller
  cancelNotice.value = null
  const result = await store.cancelRun(controller.signal)
  if (cancellationController === controller) cancellationController = null
  if (controller.signal.aborted) return
  if (result) {
    cancelNotice.value =
      result.outcome === 'cancelled'
        ? '已记录取消请求；正在执行的工作会协作式停止，可能需要少量时间。'
        : '运行已进入终态，无需取消。'
  } else if (store.currentRun && activeRunStatus[store.currentRun.status] && store.activeRunKey) {
    beginPolling(store.activeRunKey)
  }
}

function statusLabel(status: ReviewedRunStatus): string {
  const labels: Record<ReviewedRunStatus, string> = {
    queued: '排队中',
    running: '运行中',
    succeeded: '成功',
    failed: '失败',
    cancelled: '已取消',
  }
  return labels[status]
}

async function loadJobs(): Promise<void> {
  jobsController?.abort()
  const controller = new AbortController()
  jobsController = controller
  await store.loadJobs(controller.signal)
  if (jobsController === controller) jobsController = null
  if (controller.signal.aborted) return
  if (!selectedJob.value && store.runnableJobs.length > 0) {
    selectedJobKey.value = jobOptionValue(store.runnableJobs[0])
  }
}

watch(
  () => [authStore.isAuthenticated, authStore.isAdmin],
  ([authenticated, admin]) => {
    if (authenticated && admin) return
    stopAllRequests()
    store.reset()
    void router.replace(authenticated ? '/' : '/login')
  }
)

onMounted(() => {
  if (authStore.isAuthenticated && authStore.isAdmin) void loadJobs()
})

onBeforeUnmount(() => {
  stopAllRequests()
  store.reset()
})
</script>

<template>
  <section class="reviewed-runs-view" aria-labelledby="reviewed-runs-title">
    <header class="page-header">
      <div>
        <h1 id="reviewed-runs-title">审核任务运行</h1>
        <p>只提交已审核且启用的登记任务。页面不会执行脚本或连接外部数据源。</p>
      </div>
      <el-button :loading="store.jobsLoading" @click="loadJobs">刷新任务目录</el-button>
    </header>

    <el-alert
      v-if="store.jobsError"
      :title="store.jobsError"
      type="warning"
      :closable="false"
      show-icon
      role="alert"
    />
    <el-alert
      v-else-if="!store.jobsLoading && store.jobs.length === 0"
      title="当前没有任务登记在审核目录中。请确认目录配置后再提交运行。"
      type="info"
      :closable="false"
      show-icon
    />

    <el-card class="run-card">
      <template #header>
        <h2>提交审核任务</h2>
      </template>

      <el-form label-position="top" @submit.prevent="submitRun">
        <el-form-item label="审核任务">
          <el-select
            v-model="selectedJobKey"
            placeholder="选择已审核且启用的任务"
            :disabled="store.jobsLoading || store.runnableJobs.length === 0"
            @change="handleJobChange"
          >
            <el-option
              v-for="job in store.jobs"
              :key="jobOptionValue(job)"
              :label="`${job.dataset_id} · ${job.provider}/${job.model} · v${job.version}${job.reviewed ? '' : '（未审核）'}${job.enabled ? '' : '（已停用）'}`"
              :value="jobOptionValue(job)"
              :disabled="!job.reviewed || !job.enabled"
            />
          </el-select>
          <p v-if="store.jobs.length > 0 && store.runnableJobs.length === 0" class="hint">
            目录中的任务均未审核或已停用，当前不能提交运行。
          </p>
        </el-form-item>

        <el-form-item label="query（JSON 对象，最多 8192 字符）">
          <el-input
            v-model="queryText"
            type="textarea"
            :rows="5"
            :maxlength="MAX_JSON_LENGTH"
            resize="vertical"
            autocomplete="off"
            @input="handleIntentInput"
          />
        </el-form-item>

        <el-form-item label="partition（JSON 对象，最多 8192 字符）">
          <el-input
            v-model="partitionText"
            type="textarea"
            :rows="4"
            :maxlength="MAX_JSON_LENGTH"
            resize="vertical"
            autocomplete="off"
            @input="handleIntentInput"
          />
        </el-form-item>

        <el-alert
          v-if="store.pendingIntentKey"
          type="info"
          :closable="false"
          title="上次提交结果未确认。再次手动提交相同内容会复用同一幂等键；不会自动重提。"
          class="intent-alert"
        >
          <code>{{ store.pendingIntentKey }}</code>
        </el-alert>

        <p v-if="formError" class="error-text" role="alert">{{ formError }}</p>
        <p v-if="store.runError" class="error-text" role="alert">{{ store.runError }}</p>
        <el-button
          type="primary"
          :loading="store.submitting"
          :disabled="!selectedJob || store.jobsLoading"
          native-type="submit"
        >
          {{ store.pendingIntentKey ? '手动重试同一提交' : '提交运行' }}
        </el-button>
      </el-form>
    </el-card>

    <el-card class="run-card">
      <template #header>
        <h2>查询运行状态</h2>
      </template>
      <div class="lookup-row">
        <el-input
          v-model="runKeyText"
          aria-label="运行键"
          placeholder="粘贴 64 位小写十六进制运行键"
          maxlength="64"
          autocomplete="off"
        />
        <el-button :loading="store.runLoading" @click="trackRunKey">查询</el-button>
        <el-button v-if="store.activeRunKey" :loading="store.runLoading" @click="refreshRun">
          手动刷新
        </el-button>
      </div>

      <p v-if="store.runError" class="error-text" role="alert">{{ store.runError }}</p>
      <p v-if="pollNotice" class="hint" role="status">{{ pollNotice }}</p>
      <p v-if="cancelNotice" class="hint" role="status">{{ cancelNotice }}</p>

      <dl v-if="store.currentRun" class="run-details">
        <div>
          <dt>运行键</dt>
          <dd>
            <code>{{ store.currentRun.run_key }}</code>
          </dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{{ statusLabel(store.currentRun.status) }}</dd>
        </div>
        <div>
          <dt>创建时间</dt>
          <dd>{{ store.currentRun.created_at }}</dd>
        </div>
        <div v-if="store.currentRun.started_at">
          <dt>开始时间</dt>
          <dd>{{ store.currentRun.started_at }}</dd>
        </div>
        <div v-if="store.currentRun.finished_at">
          <dt>结束时间</dt>
          <dd>{{ store.currentRun.finished_at }}</dd>
        </div>
        <div v-if="store.currentRun.failure_code">
          <dt>安全错误码</dt>
          <dd>{{ store.currentRun.failure_code }}</dd>
        </div>
      </dl>
      <el-button
        v-if="isActiveRun"
        type="danger"
        :loading="store.cancelling"
        @click="cancelCurrentRun"
      >
        请求取消
      </el-button>
      <p v-if="store.currentRun?.status === 'failed'" class="hint">
        运行失败。页面只显示安全错误码，不展示内部堆栈或敏感信息。
      </p>
    </el-card>
  </section>
</template>

<style scoped>
.reviewed-runs-view {
  display: grid;
  gap: 20px;
  max-width: 1000px;
  margin: 0 auto;
}

.page-header,
.lookup-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.page-header h1,
.run-card h2 {
  margin: 0;
}

.page-header p,
.hint {
  color: var(--text-color-secondary, #606266);
}

.run-card :deep(.el-select),
.lookup-row :deep(.el-input) {
  width: 100%;
}

.intent-alert {
  margin: 0 0 16px;
}

.intent-alert code,
.run-details code {
  overflow-wrap: anywhere;
}

.error-text {
  color: var(--el-color-danger);
}

.run-details {
  display: grid;
  gap: 8px;
  margin: 20px 0;
}

.run-details div {
  display: grid;
  grid-template-columns: 130px minmax(0, 1fr);
  gap: 12px;
}

.run-details dt {
  font-weight: 600;
}

.run-details dd {
  min-width: 0;
  margin: 0;
}

@media (max-width: 640px) {
  .page-header,
  .lookup-row {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
