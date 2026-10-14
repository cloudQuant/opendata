import { expect, test, type Request, type Response } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'

type TraceEvent = {
  method: string
  path: string
  occurred_at_epoch_ms: number
  status?: number
  request_body?: unknown
  response_body?: unknown
}

const trace: TraceEvent[] = []
const blockedExternal: string[] = []
const browserErrors: string[] = []
let finalTraceWritten = false
const clientNavigation: Array<{
  action: string
  pathname_after_5s: string
  pathname_after_30s: string
  initial_observation_ms: number
  total_observation_ms: number
  ui_navigated_by_5s: boolean
  ui_navigated_to_executions: boolean
  route_events: unknown[]
  router_errors: unknown[]
  toast_events: unknown[]
  lazy_module_requests: unknown[]
}> = []
const uiErrorCases: Array<Record<string, unknown>> = []
const lazyModuleEvents: Array<{
  path: string
  started_at_epoch_ms: number
  response_status?: number
  response_elapsed_ms?: number
}> = []
const lazyModuleRequests = new Map<Request, (typeof lazyModuleEvents)[number]>()
const evidenceDir = process.env.OPENDATA_AC21_EVIDENCE_DIR
if (!evidenceDir) throw new Error('OPENDATA_AC21_EVIDENCE_DIR is required')

function pathOnly(url: string): string {
  const parsed = new URL(url)
  return `${parsed.pathname}${parsed.search}`
}

function isLoopback(url: string): boolean {
  const hostname = new URL(url).hostname
  return hostname === '127.0.0.1' || hostname === 'localhost' || hostname === '[::1]'
}

function redactBrowserText(value: string): string {
  return value.replace(/Bearer\s+[^\s]+/gi, 'Bearer [REDACTED]')
}

function addRequest(request: Request): void {
  const url = request.url()
  const parsed = new URL(url)
  if (parsed.pathname.endsWith('/src/views/ExecutionsView.vue')) {
    const event = {
      path: parsed.pathname,
      started_at_epoch_ms: Date.now(),
    }
    lazyModuleEvents.push(event)
    lazyModuleRequests.set(request, event)
  }
  if (!parsed.pathname.includes('/api/v1/')) return
  const isLoginRequest = parsed.pathname === '/api/v1/auth/login'
  const body = request.method() === 'POST' && !isLoginRequest ? request.postDataJSON() : undefined
  trace.push({
    method: request.method(),
    path: pathOnly(url),
    occurred_at_epoch_ms: Date.now(),
    request_body: body,
  })
}

function addResponse(response: Response): void {
  const request = response.request()
  const url = request.url()
  const parsed = new URL(url)
  const lazyModuleEvent = lazyModuleRequests.get(request)
  if (lazyModuleEvent) {
    const timing = request.timing()
    lazyModuleEvent.response_status = response.status()
    lazyModuleEvent.response_elapsed_ms =
      timing.requestStart >= 0 && timing.responseEnd >= timing.requestStart
        ? timing.responseEnd - timing.requestStart
        : Date.now() - lazyModuleEvent.started_at_epoch_ms
  }
  if (!parsed.pathname.includes('/api/v1/')) return
  const isLoginRequest = parsed.pathname === '/api/v1/auth/login'
  const body = request.method() === 'POST' && !isLoginRequest ? request.postDataJSON() : undefined
  trace.push({
    method: request.method(),
    path: pathOnly(url),
    occurred_at_epoch_ms: Date.now(),
    status: response.status(),
    request_body: body,
  })
}

async function installLoopbackGuard(page: import('@playwright/test').Page): Promise<void> {
  await page.addInitScript(() => {
    const probe = {
      click_started_at_ms: null as number | null,
      route_events: [] as unknown[],
      router_errors: [] as unknown[],
      toast_events: [] as unknown[],
    }
    ;(window as any).__AC21_ROUTER_PROBE__ = probe

    const collectMessages = () => {
      for (const node of Array.from(document.querySelectorAll('.el-message__content'))) {
        const message = node.textContent?.trim()
        if (!message) continue
        const previous = probe.toast_events[probe.toast_events.length - 1] as
          | { message?: string }
          | undefined
        if (previous?.message === message) continue
        probe.toast_events.push({ at_ms: performance.now(), message })
      }
    }
    const observer = new MutationObserver(collectMessages)
    const observeBody = () => {
      if (document.documentElement) {
        observer.observe(document.documentElement, {
          childList: true,
          subtree: true,
          characterData: true,
        })
      }
    }
    observeBody()
    document.addEventListener('DOMContentLoaded', observeBody, { once: true })
  })
  await page.route('**/*', async (route) => {
    const url = route.request().url()
    if (isLoopback(url)) {
      await route.continue()
      return
    }
    blockedExternal.push(new URL(url).origin)
    await route.abort('blockedbyclient')
  })
  page.on('request', addRequest)
  page.on('response', addResponse)
  page.on('console', (message) => {
    if (message.type() === 'error') browserErrors.push(redactBrowserText(message.text()))
  })
  page.on('pageerror', (error) => browserErrors.push(redactBrowserText(error.message)))
}

async function readNavigationProbe(
  page: import('@playwright/test').Page,
  clickStartedAtMs: number | null
): Promise<{ routeEvents: unknown[]; routerErrors: unknown[]; toastEvents: unknown[] }> {
  return page.evaluate((startedAt) => {
    const probe = (window as any).__AC21_ROUTER_PROBE__
    const after = (event: { at_ms?: number }) =>
      startedAt === null || (event.at_ms ?? 0) >= startedAt
    return {
      routeEvents: probe?.route_events?.filter(after) ?? [],
      routerErrors: probe?.router_errors?.filter(after) ?? [],
      toastEvents: probe?.toast_events?.filter(after) ?? [],
    }
  }, clickStartedAtMs)
}

async function observeUiNavigationAndOpenExecutions(
  page: import('@playwright/test').Page,
  action: string,
  clickedAtEpochMs: number,
  observeToThirtySeconds = false
): Promise<void> {
  const startedAtEpochMs = Date.now()
  const clickStartedAtMs = await page.evaluate(
    () => (window as any).__AC21_ROUTER_PROBE__?.click_started_at_ms ?? null
  )
  try {
    await page.waitForURL(/\/executions$/, { timeout: 5000 })
  } catch {
    // Keep the original five-second observation separate from the longer bound.
  }
  const remainingInitialObservation = 5000 - (Date.now() - startedAtEpochMs)
  if (remainingInitialObservation > 0) {
    await page.waitForTimeout(remainingInitialObservation)
  }
  const pathnameAfter5s = new URL(page.url()).pathname
  const initialObservationMs = Date.now() - startedAtEpochMs
  const uiNavigatedBy5s = pathnameAfter5s === '/executions'
  let uiNavigated = uiNavigatedBy5s
  if (!uiNavigated && observeToThirtySeconds) {
    try {
      await page.waitForURL(/\/executions$/, { timeout: 25000 })
      uiNavigated = true
    } catch {
      // Preserve a bounded 30-second natural-navigation result before fallback.
    }
  }
  const pathnameAfter30s = new URL(page.url()).pathname
  const navigationProbe = await readNavigationProbe(page, clickStartedAtMs)
  clientNavigation.push({
    action,
    pathname_after_5s: pathnameAfter5s,
    pathname_after_30s: pathnameAfter30s,
    initial_observation_ms: initialObservationMs,
    total_observation_ms: Date.now() - startedAtEpochMs,
    ui_navigated_by_5s: uiNavigatedBy5s,
    ui_navigated_to_executions: uiNavigated,
    route_events: navigationProbe.routeEvents,
    router_errors: navigationProbe.routerErrors,
    toast_events: navigationProbe.toastEvents,
    lazy_module_requests: lazyModuleEvents.filter(
      (event) => event.started_at_epoch_ms >= clickedAtEpochMs
    ),
  })
  // This explicit navigation is only a continuation path for backend-state
  // observation; it is never counted as natural UI navigation evidence.
  if (!uiNavigated) await page.goto('/executions', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('.executions-view')).toBeVisible()
}

test.afterEach(async ({ page }) => {
  if (finalTraceWritten) return
  await mkdir(dirname(`${evidenceDir}/browser-trace.json`), { recursive: true })
  await writeFile(
    `${evidenceDir}/browser-trace.json`,
    JSON.stringify(
      {
        api_events: trace,
        blocked_external_origins: blockedExternal,
        browser_errors: browserErrors,
        client_navigation: clientNavigation,
        ui_error_cases: uiErrorCases,
        lazy_module_requests: lazyModuleEvents,
        final_url: page.url(),
        credentials_recorded: false,
      },
      null,
      2
    ),
    'utf8'
  )
  finalTraceWritten = true
  try {
    await page.screenshot({ path: `${evidenceDir}/browser-final.png`, fullPage: true })
  } catch {
    // Preserve the primary Playwright failure when the browser has already closed.
  }
})

async function seedAuth(
  page: import('@playwright/test').Page,
  token: string,
  userId: number
): Promise<void> {
  await page.addInitScript(
    ({ suppliedToken, suppliedUserId }) => {
      const seededKey = '__AC21_AUTH_SEEDED__'
      if (window.sessionStorage.getItem(seededKey) === '1') return
      window.localStorage.setItem(
        'auth',
        JSON.stringify({
          user: {
            id: suppliedUserId,
            email: `ac21-${suppliedUserId}@example.com`,
            username: `ac21-${suppliedUserId}`,
            role: 'admin',
            is_active: true,
          },
          accessToken: suppliedToken,
          refreshToken: null,
        })
      )
      window.sessionStorage.setItem(seededKey, '1')
    },
    { suppliedToken: token, suppliedUserId: userId }
  )
}

async function chooseInterface(
  page: import('@playwright/test').Page,
  displayName: string
): Promise<void> {
  const selector = page.getByTestId('download-interface-select')
  await expect(selector).toBeVisible()
  await selector.click()
  await page.getByRole('option').filter({ hasText: displayName }).click()
  await expect(selector).toContainText(displayName)
}

async function startDownload(
  page: import('@playwright/test').Page,
  displayName: string
): Promise<{ executionId: number; status: number; body: unknown; clickedAtEpochMs: number }> {
  await chooseInterface(page, displayName)
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/v1/data/download'
  )
  const clickedAtEpochMs = Date.now()
  await page.evaluate(() => {
    const probe = (window as any).__AC21_ROUTER_PROBE__
    if (probe) probe.click_started_at_ms = performance.now()
  })
  await page.getByRole('button', { name: '创建下载任务' }).click()
  const response = await responsePromise
  const body = await response.json()
  trace.push({
    method: 'POST',
    path: '/api/v1/data/download#ui-click-result',
    status: response.status(),
    response_body: body,
  })
  return {
    executionId: Number(body.execution_id),
    status: response.status(),
    body,
    clickedAtEpochMs,
  }
}

async function observeExecutionStatus(
  page: import('@playwright/test').Page,
  executionId: number,
  expected: 'completed' | 'failed' | 'pending'
): Promise<string | null> {
  const token = process.env.OPENDATA_AC21_ACTIVE_TOKEN
  if (!token) throw new Error('AC21 active JWT was not provided')
  const observed = await page.evaluate(
    async ({ wantedId, expectedStatus, suppliedToken }) => {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        const response = await fetch('/api/v1/executions/?page=1&page_size=100', {
          headers: { Authorization: `Bearer ${suppliedToken}` },
        })
        const payload = await response.json()
        const rows = payload?.data?.items ?? []
        const match = rows.find((row: { id: number }) => row.id === wantedId)
        if (match?.status === expectedStatus) return expectedStatus
        if (match && expectedStatus === 'completed' && match.status === 'pending') return 'pending'
        await new Promise((resolve) => window.setTimeout(resolve, 100))
      }
      return null
    },
    { wantedId: executionId, expectedStatus: expected, suppliedToken: token }
  )
  return observed
}

async function observeDisplayedStatus(
  page: import('@playwright/test').Page,
  executionId: number,
  expected: 'completed' | 'failed' | 'pending',
  clickedAtEpochMs: number
): Promise<number> {
  const statusLabels = { completed: '成功', failed: '失败', pending: '等待中' }
  const row = page.getByRole('row').filter({
    has: page.getByRole('cell', { name: String(executionId), exact: true }),
  })
  await expect(row).toContainText(statusLabels[expected])
  return Date.now() - clickedAtEpochMs
}

async function readExecutionCount(
  page: import('@playwright/test').Page,
  token: string
): Promise<number> {
  const result = await page.evaluate(
    async (suppliedToken) => {
      const response = await fetch('/api/v1/executions/?page=1&page_size=100', {
        headers: { Authorization: `Bearer ${suppliedToken}` },
      })
      const payload = await response.json()
      return { status: response.status, total: payload?.data?.total }
    },
    token
  )
  expect(result.status).toBe(200)
  expect(Number.isSafeInteger(result.total)).toBe(true)
  return result.total as number
}

type UiErrorMutation = 'unauthenticated' | 'inactive_user' | 'missing_interface_id' | 'unknown_interface'

async function clickDownloadError(
  page: import('@playwright/test').Page,
  options: {
    label: string
    displayName: string
    mutation: UiErrorMutation
    expectedStatus: number
    expectedFeedback: string
    activeToken: string
    inactiveToken: string
  }
): Promise<void> {
  await chooseInterface(page, options.displayName)
  const taskCountBefore = await readExecutionCount(page, options.activeToken)
  const startedAtEpochMs = Date.now()
  const routeStartedAtMs = await page.evaluate(() => performance.now())
  let forwardedBody: unknown
  let originalBody: unknown
  let requestMutation = ''
  const routeHandler = async (route: import('@playwright/test').Route) => {
    const request = route.request()
    const headers = { ...request.headers() }
    const body = request.postDataJSON() as Record<string, unknown>
    originalBody = { ...body }
    if (options.mutation === 'unauthenticated') {
      delete headers.authorization
      delete headers.Authorization
      requestMutation = 'Authorization omitted for this real Axios request'
    } else if (options.mutation === 'inactive_user') {
      headers.authorization = `Bearer ${options.inactiveToken}`
      requestMutation = 'Authorization replaced with test-owned inactive-user JWT'
    } else if (options.mutation === 'missing_interface_id') {
      delete body.interface_id
      forwardedBody = body
      requestMutation = 'interface_id removed from this real Axios request body'
    } else {
      body.interface_id = 999999
      forwardedBody = body
      requestMutation = 'interface_id changed to unknown test object ID 999999'
    }
    if (forwardedBody === undefined) forwardedBody = body
    await route.continue({
      headers,
      ...(options.mutation === 'missing_interface_id' || options.mutation === 'unknown_interface'
        ? { postData: JSON.stringify(body) }
        : {}),
    })
  }
  await page.route('**/api/v1/data/download', routeHandler)
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/v1/data/download'
  )
  const clickStartedAtEpochMs = Date.now()
  await page.evaluate(() => {
    const probe = (window as any).__AC21_ROUTER_PROBE__
    if (probe) probe.click_started_at_ms = performance.now()
  })
  await page.getByRole('button', { name: '创建下载任务' }).click()
  const response = await responsePromise
  await page.unroute('**/api/v1/data/download', routeHandler)
  let responseBody: unknown = null
  try {
    responseBody = await response.json()
  } catch {
    responseBody = null
  }

  if (options.mutation === 'unauthenticated') {
    await page.waitForURL(/\/login(?:\?.*)?$/, { timeout: 10000 }).catch(() => undefined)
    await expect(page.locator('[data-testid="session-expired-message"]')).toContainText(
      '登录已失效，请重新登录'
    )
  } else {
    await page
      .locator('.el-message--error .el-message__content')
      .first()
      .waitFor({ state: 'visible', timeout: 2000 })
      .catch(() => undefined)
  }
  const errorMessages = await page.locator('.el-message--error .el-message__content').allTextContents()
  const sessionMessage = await page
    .locator('[data-testid="session-expired-message"]')
    .allTextContents()
  const visibleFeedback = [...errorMessages, ...sessionMessage].map((value) => value.trim()).filter(Boolean)
  const successToastVisible = await page.locator('.el-message--success').isVisible().catch(() => false)
  const taskCountAfter = await readExecutionCount(page, options.activeToken)
  const finishedAtEpochMs = Date.now()
  const feedbackMatched = visibleFeedback.some((message) => message.includes(options.expectedFeedback))
  const observation = {
    label: options.label,
    entrypoint: 'ScriptDetailView.handleDownload -> dataApi.download -> Axios -> actual FastAPI route',
    expected_http: options.expectedStatus,
    observed_http: response.status(),
    request_mutation: requestMutation,
    ui_request_body_before_mutation: originalBody,
    forwarded_request_body: forwardedBody,
    response_body: responseBody,
    expected_feedback: options.expectedFeedback,
    visible_feedback: visibleFeedback,
    feedback_matched: feedbackMatched,
    success_toast_visible: successToastVisible,
    pathname_after: new URL(page.url()).pathname,
    task_count_before: taskCountBefore,
    task_count_after: taskCountAfter,
    expected_task_delta: 0,
    observed_task_delta: taskCountAfter - taskCountBefore,
    started_at_epoch_ms: startedAtEpochMs,
    route_started_at_performance_ms: routeStartedAtMs,
    click_started_at_epoch_ms: clickStartedAtEpochMs,
    finished_at_epoch_ms: finishedAtEpochMs,
    credentials_recorded: false,
  }
  uiErrorCases.push(observation)
  trace.push({
    method: 'POST',
    path: `/api/v1/data/download#ui-error-${options.label}`,
    status: response.status(),
    request_body: forwardedBody,
    response_body: responseBody,
  })
  await page.screenshot({
    path: `${evidenceDir}/ui-error-${options.label}.png`,
    fullPage: true,
  })

  expect(response.status(), `${options.label}: actual backend HTTP status`).toBe(options.expectedStatus)
}

async function submitInvalidCredentials(
  page: import('@playwright/test').Page,
  activeToken: string
): Promise<Record<string, unknown>> {
  await expect(page).toHaveURL(/\/login\?reason=session_expired$/)
  const persistentNotice = page.locator('[data-testid="session-expired-message"]')
  await expect(persistentNotice).toContainText('登录已失效，请重新登录')
  const taskCountBefore = await readExecutionCount(page, activeToken)
  const startedAtEpochMs = Date.now()
  const navigationEvents: string[] = []
  const apiRequests: string[] = []
  const onFrameNavigated = (frame: import('@playwright/test').Frame) => {
    if (frame === page.mainFrame()) navigationEvents.push(new URL(frame.url()).pathname)
  }
  const onRequest = (request: Request) => {
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/v1/')) apiRequests.push(`${request.method()} ${url.pathname}`)
  }
  page.on('framenavigated', onFrameNavigated)
  page.on('request', onRequest)

  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/v1/auth/login'
  )
  await page.getByPlaceholder('请输入邮箱').fill('ac21-active@example.com')
  await page.getByPlaceholder('请输入密码').fill('wrong-ac21-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  const response = await responsePromise
  // Playwright's failure context may snapshot form controls; clear the
  // test-only password before any subsequent assertion can fail.
  await page.getByPlaceholder('请输入密码').fill('')
  let responseBody: unknown = null
  try {
    responseBody = await response.json()
  } catch {
    responseBody = null
  }
  const loginFailure = page
    .locator('.el-message--error .el-message__content')
    .filter({ hasText: '邮箱或密码错误' })
    .last()
  await expect(loginFailure).toBeVisible()
  await expect(persistentNotice).toContainText('登录已失效，请重新登录')
  await expect(page).toHaveURL(/\/login\?reason=session_expired$/)
  const actionApiRequests = [...apiRequests]
  expect(actionApiRequests).toEqual(['POST /api/v1/auth/login'])
  const taskCountAfter = await readExecutionCount(page, activeToken)
  const navigationDelta = navigationEvents.length
  page.off('framenavigated', onFrameNavigated)
  page.off('request', onRequest)

  const observation = {
    label: 'invalid_login_credentials_401',
    expected_http: 401,
    observed_http: response.status(),
    response_body: responseBody,
    visible_feedback: await loginFailure.allTextContents(),
    persistent_session_expired_notice_visible: await persistentNotice.isVisible(),
    pathname_after: new URL(page.url()).pathname,
    query_after: new URL(page.url()).search,
    navigation_count_during_request: navigationDelta,
    api_requests_during_request: actionApiRequests,
    task_count_before: taskCountBefore,
    task_count_after: taskCountAfter,
    expected_task_delta: 0,
    observed_task_delta: taskCountAfter - taskCountBefore,
    started_at_epoch_ms: startedAtEpochMs,
    finished_at_epoch_ms: Date.now(),
    request_body_recorded: false,
    credentials_recorded: false,
  }
  await page.screenshot({
    path: `${evidenceDir}/ui-error-invalid-login-credentials-401.png`,
    fullPage: true,
  })
  return observation
}

test('real browser download chain records API, task, auth and storage evidence', async ({
  page,
}) => {
  const activeToken = process.env.OPENDATA_AC21_ACTIVE_TOKEN
  const inactiveToken = process.env.OPENDATA_AC21_INACTIVE_TOKEN
  if (!activeToken || !inactiveToken) throw new Error('AC21 session JWT inputs are missing')
  const uiStatusDisplayMs: Record<string, number> = {}
  const errorsOnly = process.env.OPENDATA_AC21_ERRORS_ONLY === '1'
  const observedStatus: Record<string, string | null> = {}
  const uiClicks: Array<Record<string, unknown>> = []
  let loginErrorCounterfact: Record<string, unknown> | null = null

  await installLoopbackGuard(page)
  await seedAuth(page, activeToken, 71001)
  await page.goto('/scripts/7', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('.script-detail-view')).toBeVisible()
  await mkdir(dirname(`${evidenceDir}/browser-trace.json`), { recursive: true })
  await writeFile(
    `${evidenceDir}/initial-load-diagnostic.json`,
    JSON.stringify(
      {
        url: page.url(),
        body_text: await page.locator('body').innerText(),
        api_events: trace,
        credentials_recorded: false,
      },
      null,
      2
    ),
    'utf8'
  )
  await page.screenshot({ path: `${evidenceDir}/initial-load.png`, fullPage: true })
  await expect(
    page.getByRole('cell', { name: 'AC21_SCRIPT_NUMERIC_COLLISION', exact: true })
  ).toBeVisible()
  await expect(page.getByTestId('download-interface-select')).toBeVisible()

  if (!errorsOnly) {
  // Script ID 7 intentionally collides with an interface ID. A real UI click
  // must dispatch the selected DataInterface ID 91, not the script ID 7.
  const success = await startDownload(page, 'AC21_SELECTED_INTERFACE_91')
  expect(success.status).toBe(202)
  expect(success.executionId).toBeGreaterThan(0)
  await observeUiNavigationAndOpenExecutions(
    page,
    'success_nonempty',
    success.clickedAtEpochMs,
    true
  )
  const successObservedStatus = await observeExecutionStatus(page, success.executionId, 'completed')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.locator('.executions-view')).toBeVisible()
  uiStatusDisplayMs.nonempty_success = await observeDisplayedStatus(
    page,
    success.executionId,
    'completed',
    success.clickedAtEpochMs
  )
  await page.screenshot({ path: `${evidenceDir}/success-nonempty-task.png`, fullPage: true })

  // The real source adapter raises for interface 92; the handler still returns
  // 202 and the actual background execution must persist as failed.
  await page.goto('/scripts/7')
  await expect(page.getByTestId('download-interface-select')).toBeVisible()
  const failure = await startDownload(page, 'AC21_FAILURE_INTERFACE_92')
  expect(failure.status).toBe(202)
  expect(failure.executionId).toBeGreaterThan(0)
  await observeUiNavigationAndOpenExecutions(page, 'source_failure', failure.clickedAtEpochMs)
  const failureObservedStatus = await observeExecutionStatus(page, failure.executionId, 'failed')
  await page.reload({ waitUntil: 'domcontentloaded' })
  uiStatusDisplayMs.source_failure = await observeDisplayedStatus(
    page,
    failure.executionId,
    'failed',
    failure.clickedAtEpochMs
  )
  await page.screenshot({ path: `${evidenceDir}/failure-task.png`, fullPage: true })

  await page.goto('/scripts/7')
  await expect(page.getByTestId('download-interface-select')).toBeVisible()
  const storageFailure = await startDownload(page, 'AC21_STORAGE_FAILURE_INTERFACE_93')
  expect(storageFailure.status).toBe(202)
  await observeUiNavigationAndOpenExecutions(
    page,
    'storage_failure',
    storageFailure.clickedAtEpochMs
  )
  const storageFailureObservedStatus = await observeExecutionStatus(
    page,
    storageFailure.executionId,
    'failed'
  )
  await page.reload({ waitUntil: 'domcontentloaded' })
  uiStatusDisplayMs.storage_failure = await observeDisplayedStatus(
    page,
    storageFailure.executionId,
    'failed',
    storageFailure.clickedAtEpochMs
  )
  await page.screenshot({ path: `${evidenceDir}/failure-storage-write.png`, fullPage: true })

  await page.goto('/scripts/7')
  await expect(page.getByTestId('download-interface-select')).toBeVisible()
  const metadataFailure = await startDownload(page, 'AC21_METADATA_FAILURE_INTERFACE_94')
  expect(metadataFailure.status).toBe(202)
  await observeUiNavigationAndOpenExecutions(
    page,
    'metadata_commit_failure',
    metadataFailure.clickedAtEpochMs
  )
  const metadataFailureObservedStatus = await observeExecutionStatus(
    page,
    metadataFailure.executionId,
    'failed'
  )
  await page.reload({ waitUntil: 'domcontentloaded' })
  uiStatusDisplayMs.metadata_commit_failure = await observeDisplayedStatus(
    page,
    metadataFailure.executionId,
    'failed',
    metadataFailure.clickedAtEpochMs
  )
  await page.screenshot({ path: `${evidenceDir}/failure-metadata-commit.png`, fullPage: true })

  await page.goto('/scripts/7')
  await expect(page.getByTestId('download-interface-select')).toBeVisible()
  const empty = await startDownload(page, 'AC21_EMPTY_INTERFACE_95')
  expect(empty.status).toBe(202)
  await observeUiNavigationAndOpenExecutions(page, 'success_empty', empty.clickedAtEpochMs)
  const emptyObservedStatus = await observeExecutionStatus(page, empty.executionId, 'completed')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.locator('.executions-view')).toBeVisible()
  uiStatusDisplayMs.empty_success = await observeDisplayedStatus(
    page,
    empty.executionId,
    'completed',
    empty.clickedAtEpochMs
  )
  await page.screenshot({ path: `${evidenceDir}/success-empty-task.png`, fullPage: true })
    Object.assign(observedStatus, {
      nonempty_success: successObservedStatus,
      source_failure: failureObservedStatus,
      storage_failure: storageFailureObservedStatus,
      metadata_commit_failure: metadataFailureObservedStatus,
      empty_success: emptyObservedStatus,
    })
    uiClicks.push(
      { script_id: '7', selected_interface_id: 91, response_execution_id: success.executionId },
      { script_id: '7', selected_interface_id: 92, response_execution_id: failure.executionId },
      { script_id: '7', selected_interface_id: 93, response_execution_id: storageFailure.executionId },
      { script_id: '7', selected_interface_id: 94, response_execution_id: metadataFailure.executionId },
      { script_id: '7', selected_interface_id: 95, response_execution_id: empty.executionId }
    )
  } else {
    // The errors-only leg must still carry a verdict: it asserts its own
    // invariant (the full-chain evidence stays empty) instead of silently
    // skipping fifteen assertions whenever OPENDATA_AC21_ERRORS_ONLY=1.
    expect(uiClicks).toEqual([])
    expect(observedStatus).toEqual({})
    expect(Object.keys(uiStatusDisplayMs)).toEqual([])
  }

  await page.goto('/scripts/7', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('download-interface-select')).toBeVisible()
  await clickDownloadError(page, {
    label: 'inactive_user_403',
    displayName: 'AC21_SELECTED_INTERFACE_91',
    mutation: 'inactive_user',
    expectedStatus: 403,
    expectedFeedback: '权限不足',
    activeToken,
    inactiveToken,
  })
  await clickDownloadError(page, {
    label: 'missing_interface_id_422',
    displayName: 'AC21_SELECTED_INTERFACE_91',
    mutation: 'missing_interface_id',
    expectedStatus: 422,
    expectedFeedback: 'interface_id',
    activeToken,
    inactiveToken,
  })
  await clickDownloadError(page, {
    label: 'unknown_interface_404',
    displayName: 'AC21_SELECTED_INTERFACE_91',
    mutation: 'unknown_interface',
    expectedStatus: 404,
    expectedFeedback: '请求的资源不存在',
    activeToken,
    inactiveToken,
  })
  await clickDownloadError(page, {
    label: 'unauthenticated_401',
    displayName: 'AC21_SELECTED_INTERFACE_91',
    mutation: 'unauthenticated',
    expectedStatus: 401,
    expectedFeedback: '登录已失效，请重新登录',
    activeToken,
    inactiveToken,
  })
  loginErrorCounterfact = await submitInvalidCredentials(page, activeToken)
  expect(blockedExternal).toEqual([])

  await mkdir(dirname(`${evidenceDir}/browser-trace.json`), { recursive: true })
  await writeFile(
    `${evidenceDir}/browser-trace.json`,
    JSON.stringify(
      {
        api_events: trace,
        blocked_external_origins: blockedExternal,
        browser_errors: browserErrors,
        client_navigation: clientNavigation,
        ui_error_cases: uiErrorCases,
        lazy_module_requests: lazyModuleEvents,
        final_url: page.url(),
        ui_clicks: uiClicks,
        observed_status: observedStatus,
        login_error_counterfact: loginErrorCounterfact,
        ui_status_display_ms: uiStatusDisplayMs,
        auth_and_validation: Object.fromEntries(
          uiErrorCases.map((item) => [item.label, item.observed_http])
        ),
        credentials_recorded: false,
      },
      null,
      2
    ),
    'utf8'
  )
  finalTraceWritten = true

  expect(
    uiErrorCases.map((item) => ({
      label: item.label,
      status: item.observed_http,
      feedback_matched: item.feedback_matched,
      success_toast_visible: item.success_toast_visible,
      task_delta: item.observed_task_delta,
    })),
    'Each real UI failure must show the expected visible error and avoid task creation'
  ).toEqual([
    {
      label: 'inactive_user_403',
      status: 403,
      feedback_matched: true,
      success_toast_visible: false,
      task_delta: 0,
    },
    {
      label: 'missing_interface_id_422',
      status: 422,
      feedback_matched: true,
      success_toast_visible: false,
      task_delta: 0,
    },
    {
      label: 'unknown_interface_404',
      status: 404,
      feedback_matched: true,
      success_toast_visible: false,
      task_delta: 0,
    },
    {
      label: 'unauthenticated_401',
      status: 401,
      feedback_matched: true,
      success_toast_visible: false,
      task_delta: 0,
    },
  ])

  expect(loginErrorCounterfact).toMatchObject({
    expected_http: 401,
    observed_http: 401,
    visible_feedback: ['邮箱或密码错误'],
    persistent_session_expired_notice_visible: true,
    pathname_after: '/login',
    query_after: '?reason=session_expired',
    navigation_count_during_request: 0,
    api_requests_during_request: ['POST /api/v1/auth/login'],
    expected_task_delta: 0,
    observed_task_delta: 0,
    request_body_recorded: false,
    credentials_recorded: false,
  })

  // Counterfactual expected behavior: a nonempty stored download should reach
  // completed. This assertion is intentionally end-of-test so all probes and
  // redacted evidence are collected even when the backend fails it.
  if (!errorsOnly) {
  expect(observedStatus).toEqual({
    nonempty_success: 'completed',
    source_failure: 'failed',
    storage_failure: 'failed',
    metadata_commit_failure: 'failed',
    empty_success: 'completed',
  })
  expect(
    clientNavigation.filter((event) => !event.ui_navigated_to_executions),
    'AC21 natural UI navigation failed after an accepted real download click'
  ).toEqual([])
  } else {
    // The errors-only leg carries its own verdict instead of silence: the
    // five-chain statuses were never observed on this path.
    expect(observedStatus).toEqual({})
  }
})
