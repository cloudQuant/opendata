import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  reviewedRunsApi,
  type CancelReviewedRunResponse,
  type CreateReviewedRunRequest,
  type ReviewedJob,
  type ReviewedJobId,
  type ReviewedJobVersion,
  type ReviewedRunAccepted,
  type ReviewedRunStatus,
  type ReviewedRunView,
} from '@/api/reviewedRuns'

const RUN_KEY_PATTERN = /^[0-9a-f]{64}$/
const ACTIVE_STATUSES = new Set<ReviewedRunStatus>(['queued', 'running'])
const TERMINAL_STATUSES = new Set<ReviewedRunStatus>(['succeeded', 'failed', 'cancelled'])
const SAFE_API_ERROR_CODES = new Set([
  'reviewed_runtime_unavailable',
  'reviewed_service_unavailable',
  'reviewed_run_service_unavailable',
  'job_not_found',
  'job_not_runnable',
  'idempotency_conflict',
  'invalid_request',
  'request_too_large',
  'json_required',
  'run_not_found',
])

type JsonObject = Record<string, unknown>

interface RunIntent {
  signature: string
  idempotencyKey: string
}

interface StartRunInput {
  jobId: ReviewedJobId
  version: ReviewedJobVersion
  query: JsonObject
  partition: JsonObject
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isJobId(value: unknown): value is ReviewedJobId {
  if (typeof value === 'string') return value.trim().length > 0 && value.length <= 200
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function isVersion(value: unknown): value is ReviewedJobVersion {
  return (
    (typeof value === 'string' && value.trim().length > 0 && value.length <= 200) ||
    (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0)
  )
}

function jobIdentity(jobId: ReviewedJobId): string {
  return `${typeof jobId}:${String(jobId)}`
}

function parseJob(value: unknown): ReviewedJob {
  if (
    !isRecord(value) ||
    !isJobId(value.job_id) ||
    !isVersion(value.version) ||
    typeof value.dataset_id !== 'string' ||
    value.dataset_id.trim() === '' ||
    typeof value.provider !== 'string' ||
    value.provider.trim() === '' ||
    typeof value.model !== 'string' ||
    value.model.trim() === '' ||
    typeof value.enabled !== 'boolean' ||
    typeof value.reviewed !== 'boolean'
  ) {
    throw new Error('reviewed_job_invalid')
  }

  return {
    job_id: value.job_id,
    version: value.version,
    dataset_id: value.dataset_id,
    provider: value.provider,
    model: value.model,
    enabled: value.enabled,
    reviewed: value.reviewed,
  }
}

function parseJobs(value: unknown): ReviewedJob[] {
  if (!isRecord(value) || !Array.isArray(value.jobs)) throw new Error('reviewed_jobs_invalid')
  return value.jobs.map(parseJob)
}

function parseTimestamp(value: unknown, options: { nullable?: boolean } = {}): string | null {
  if (options.nullable && value === null) return null
  if (typeof value !== 'string' || value.trim() === '' || !Number.isFinite(Date.parse(value))) {
    throw new Error('reviewed_run_invalid')
  }
  return value
}

function parseRunStatus(value: unknown): ReviewedRunStatus {
  if (
    value === 'queued' ||
    value === 'running' ||
    value === 'succeeded' ||
    value === 'failed' ||
    value === 'cancelled'
  ) {
    return value
  }
  throw new Error('reviewed_run_status_invalid')
}

function parseRunView(value: unknown): ReviewedRunView {
  if (
    !isRecord(value) ||
    typeof value.run_key !== 'string' ||
    !RUN_KEY_PATTERN.test(value.run_key)
  ) {
    throw new Error('reviewed_run_key_invalid')
  }
  const status = parseRunStatus(value.status)
  const failureCode = value.failure_code
  if (failureCode !== null && failureCode !== 'run_failed') {
    throw new Error('reviewed_run_invalid')
  }
  return {
    run_key: value.run_key,
    status,
    created_at: parseTimestamp(value.created_at) as string,
    updated_at: parseTimestamp(value.updated_at) as string,
    started_at: parseTimestamp(value.started_at, { nullable: true }),
    finished_at: parseTimestamp(value.finished_at, { nullable: true }),
    failure_code: failureCode,
  }
}

function parseAcceptedRun(value: unknown, input: StartRunInput): ReviewedRunAccepted {
  if (
    !isRecord(value) ||
    typeof value.run_key !== 'string' ||
    !RUN_KEY_PATTERN.test(value.run_key) ||
    !isJobId(value.job_id) ||
    !isVersion(value.version) ||
    jobIdentity(value.job_id) !== jobIdentity(input.jobId) ||
    value.version !== input.version
  ) {
    throw new Error('reviewed_run_acceptance_invalid')
  }
  return {
    run_key: value.run_key,
    status: parseRunStatus(value.status),
    job_id: value.job_id,
    version: value.version,
    created_at: parseTimestamp(value.created_at) as string,
  }
}

function parseCancelResponse(value: unknown, expectedRunKey: string): CancelReviewedRunResponse {
  if (!isRecord(value) || (value.outcome !== 'cancelled' && value.outcome !== 'terminal')) {
    throw new Error('reviewed_cancel_invalid')
  }
  const run = parseRunView(value.run)
  if (
    run.run_key !== expectedRunKey ||
    (value.outcome === 'cancelled' && run.status !== 'cancelled') ||
    (value.outcome === 'terminal' && !TERMINAL_STATUSES.has(run.status))
  ) {
    throw new Error('reviewed_cancel_invalid')
  }
  return { run, outcome: value.outcome }
}

function parseJsonObject(value: unknown): value is JsonObject {
  if (!isRecord(value)) return false
  try {
    return JSON.stringify(value).length <= 16_384
  } catch {
    return false
  }
}

function createIdempotencyKey(): string {
  const secureCrypto = globalThis.crypto
  if (!secureCrypto?.getRandomValues) throw new Error('secure_idempotency_unavailable')
  const bytes = secureCrypto.getRandomValues(new Uint8Array(32))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function errorCode(error: unknown): string | null {
  if (!isRecord(error) || !isRecord(error.response) || !isRecord(error.response.data)) return null
  const detail = error.response.data.detail
  const code = typeof detail === 'string' ? detail : isRecord(detail) ? detail.code : null
  return typeof code === 'string' && SAFE_API_ERROR_CODES.has(code) ? code : null
}

function isAbort(error: unknown): boolean {
  if (!isRecord(error)) return false
  return (
    error.name === 'AbortError' || error.name === 'CanceledError' || error.code === 'ERR_CANCELED'
  )
}

function safeErrorMessage(
  error: unknown,
  fallback: string,
  options: { ambiguousSubmission?: boolean } = {}
): string {
  const code = errorCode(error)
  if (
    code === 'reviewed_runtime_unavailable' ||
    code === 'reviewed_service_unavailable' ||
    code === 'reviewed_run_service_unavailable'
  )
    return '审核运行服务尚未配置，当前无法读取或提交运行。'
  if (code === 'job_not_found') return '任务已不在审核目录中，请刷新任务目录。'
  if (code === 'job_not_runnable') return '该任务尚未审核或已停用，不能启动。'
  if (code === 'run_not_found') return '没有找到该运行，请确认运行键后重试。'
  if (code === 'invalid_request') return '请求内容无效，请检查所选任务与 JSON 参数。'
  if (code === 'request_too_large') return '请求内容超出服务允许的大小。'
  if (code === 'json_required') return '服务要求使用 JSON 格式提交请求。'
  if (code) return `请求失败（${code}）。`
  if (
    options.ambiguousSubmission &&
    errorCode(error) === null &&
    isRecord(error) &&
    error.response === undefined
  ) {
    return '请求未确认。可手动重试同一请求，系统会保留本次幂等键。'
  }
  return fallback
}

function isTerminal(status: ReviewedRunStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export const useReviewedRunsStore = defineStore('reviewed-runs', () => {
  const jobs = ref<ReviewedJob[]>([])
  const jobsLoading = ref(false)
  const jobsError = ref<string | null>(null)
  const activeRunKey = ref<string | null>(null)
  const currentRun = ref<ReviewedRunView | null>(null)
  const runLoading = ref(false)
  const runError = ref<string | null>(null)
  const submitting = ref(false)
  const cancelling = ref(false)
  const pendingIntentKey = ref<string | null>(null)

  const runnableJobs = computed(() => jobs.value.filter((job) => job.reviewed && job.enabled))

  let jobsEpoch = 0
  let runEpoch = 0
  let fetchEpoch = 0
  let submitEpoch = 0
  let cancelEpoch = 0
  let pendingIntent: RunIntent | null = null

  async function loadJobs(signal?: AbortSignal): Promise<boolean> {
    const epoch = ++jobsEpoch
    jobsLoading.value = true
    jobsError.value = null
    try {
      const response = await reviewedRunsApi.listJobs(signal)
      const parsedJobs = parseJobs(response)
      if (epoch !== jobsEpoch || signal?.aborted) return false
      jobs.value = parsedJobs
      return true
    } catch (error) {
      if (epoch !== jobsEpoch || signal?.aborted || isAbort(error)) return false
      jobs.value = []
      jobsError.value = safeErrorMessage(error, '审核任务列表加载失败。')
      return false
    } finally {
      if (epoch === jobsEpoch) jobsLoading.value = false
    }
  }

  function clearIntent(): void {
    pendingIntent = null
    pendingIntentKey.value = null
  }

  function invalidateRunContext(): void {
    runEpoch += 1
    fetchEpoch += 1
    submitEpoch += 1
    cancelEpoch += 1
    activeRunKey.value = null
    currentRun.value = null
    runLoading.value = false
    runError.value = null
    submitting.value = false
    cancelling.value = false
    clearIntent()
  }

  function selectRunKey(runKey: string): boolean {
    if (!RUN_KEY_PATTERN.test(runKey)) {
      runError.value = '运行键格式无效；请粘贴 64 位小写十六进制运行键。'
      return false
    }
    runEpoch += 1
    fetchEpoch += 1
    submitEpoch += 1
    cancelEpoch += 1
    activeRunKey.value = runKey
    currentRun.value = null
    runLoading.value = false
    runError.value = null
    submitting.value = false
    cancelling.value = false
    clearIntent()
    return true
  }

  async function startRun(
    input: StartRunInput,
    signal?: AbortSignal
  ): Promise<ReviewedRunAccepted | null> {
    const job = jobs.value.find(
      (candidate) =>
        jobIdentity(candidate.job_id) === jobIdentity(input.jobId) &&
        candidate.version === input.version
    )
    if (!job) {
      runError.value = '所选任务不在当前审核目录中，请刷新任务目录。'
      return null
    }
    if (!job.reviewed || !job.enabled) {
      runError.value = !job.reviewed ? '该任务尚未审核，不能启动。' : '该任务已停用，不能启动。'
      return null
    }
    if (!parseJsonObject(input.query) || !parseJsonObject(input.partition)) {
      runError.value = 'query 与 partition 必须是大小不超过 16 KB 的 JSON 对象。'
      return null
    }

    const intentBody = {
      job_id: input.jobId,
      version: input.version,
      query: input.query,
      partition: input.partition,
      shard: null,
    }
    const signature = JSON.stringify(intentBody)
    if (!pendingIntent || pendingIntent.signature !== signature) {
      try {
        pendingIntent = { signature, idempotencyKey: createIdempotencyKey() }
        pendingIntentKey.value = pendingIntent.idempotencyKey
      } catch {
        runError.value = '当前环境无法生成安全幂等键，未提交运行。'
        return null
      }
    }

    runEpoch += 1
    const context = runEpoch
    const requestEpoch = ++submitEpoch
    activeRunKey.value = null
    currentRun.value = null
    runLoading.value = false
    runError.value = null
    submitting.value = true
    const requestBody: CreateReviewedRunRequest = {
      ...intentBody,
      idempotency_key: pendingIntent.idempotencyKey,
    }
    try {
      const response = await reviewedRunsApi.createRun(requestBody, signal)
      const accepted = parseAcceptedRun(response, input)
      if (context !== runEpoch || requestEpoch !== submitEpoch || signal?.aborted) return null
      activeRunKey.value = accepted.run_key
      clearIntent()
      return accepted
    } catch (error) {
      if (
        context !== runEpoch ||
        requestEpoch !== submitEpoch ||
        signal?.aborted ||
        isAbort(error)
      ) {
        return null
      }
      runError.value = safeErrorMessage(error, '提交未确认；可用相同幂等键手动重试。', {
        ambiguousSubmission: true,
      })
      return null
    } finally {
      if (context === runEpoch && requestEpoch === submitEpoch) submitting.value = false
    }
  }

  async function fetchRun(runKey: string, signal?: AbortSignal): Promise<ReviewedRunView | null> {
    if (!RUN_KEY_PATTERN.test(runKey) || activeRunKey.value !== runKey) return null
    const context = runEpoch
    const requestEpoch = ++fetchEpoch
    runLoading.value = true
    runError.value = null
    try {
      const response = await reviewedRunsApi.getRun(runKey, signal)
      const run = parseRunView(response)
      if (
        context !== runEpoch ||
        requestEpoch !== fetchEpoch ||
        activeRunKey.value !== runKey ||
        signal?.aborted ||
        run.run_key !== runKey
      ) {
        return null
      }
      currentRun.value = run
      return run
    } catch (error) {
      if (
        context !== runEpoch ||
        requestEpoch !== fetchEpoch ||
        signal?.aborted ||
        isAbort(error)
      ) {
        return null
      }
      runError.value = safeErrorMessage(error, '读取运行状态失败。')
      return null
    } finally {
      if (context === runEpoch && requestEpoch === fetchEpoch) runLoading.value = false
    }
  }

  async function cancelRun(signal?: AbortSignal): Promise<CancelReviewedRunResponse | null> {
    const runKey = activeRunKey.value
    const current = currentRun.value
    if (!runKey || !current || !ACTIVE_STATUSES.has(current.status)) {
      runError.value = '只有排队中或运行中的任务可以取消。'
      return null
    }
    const context = runEpoch
    const requestEpoch = ++cancelEpoch
    cancelling.value = true
    runError.value = null
    try {
      const response = parseCancelResponse(await reviewedRunsApi.cancelRun(runKey, signal), runKey)
      if (
        context !== runEpoch ||
        requestEpoch !== cancelEpoch ||
        activeRunKey.value !== runKey ||
        signal?.aborted
      ) {
        return null
      }
      currentRun.value = response.run
      return response
    } catch (error) {
      if (
        context !== runEpoch ||
        requestEpoch !== cancelEpoch ||
        signal?.aborted ||
        isAbort(error)
      ) {
        return null
      }
      runError.value = safeErrorMessage(error, '取消请求失败。')
      return null
    } finally {
      if (context === runEpoch && requestEpoch === cancelEpoch) cancelling.value = false
    }
  }

  function reset(): void {
    jobsEpoch += 1
    fetchEpoch += 1
    jobs.value = []
    jobsLoading.value = false
    jobsError.value = null
    invalidateRunContext()
  }

  return {
    jobs,
    runnableJobs,
    jobsLoading,
    jobsError,
    activeRunKey,
    currentRun,
    runLoading,
    runError,
    submitting,
    cancelling,
    pendingIntentKey,
    loadJobs,
    clearIntent,
    invalidateRunContext,
    selectRunKey,
    startRun,
    fetchRun,
    cancelRun,
    reset,
    isTerminal,
  }
})

export type { StartRunInput }
