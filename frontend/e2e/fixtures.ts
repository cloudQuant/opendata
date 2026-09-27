import { type Page } from '@playwright/test'

/**
 * Per-page HTTP fixtures for the e2e plane.
 *
 * Every body here was copied from the handler that produces it, not from what a
 * page would like to receive. The census lives in
 * `docs/evidence/C32/frontend-request-census.txt` (which endpoint each page
 * really requests) and `docs/evidence/C32/endpoint-payload-shapes.txt` (the key
 * set of each reply). Two shapes are load-bearing and easy to get wrong:
 *
 *   * Most endpoints answer `APIResponse{success,message,data}` — the axios
 *     interceptor at src/utils/request.ts hands the caller `res.data ?? res`, so
 *     a fixture that forgets the envelope still renders, and the test then
 *     proves nothing about the real contract. `envelope()` makes it explicit.
 *   * Three of the twelve measured endpoints answer a BARE object with no
 *     envelope at all — `GET /tables/{id}/schema`, `GET /settings/database` and
 *     `GET /settings/database/warehouse` (endpoint-payload-shapes.txt lists all
 *     14 bare handlers; these three are the ones on measured pages). Only the
 *     schema is on a page this plane visits, and it is stubbed bare for contract
 *     fidelity. It is worth knowing that no assertion here could enforce that:
 *     wrapping the schema stub in an envelope leaves the page reading the same
 *     object, because the interceptor resolves `res.data ?? res` either way
 *     (measured — `docs/evidence/C32/falsification.txt`, case E). The shape is
 *     pinned by reading the handlers, not by the browser.
 *
 * `stubApi` registers one catch-all route and refuses to answer anything it was
 * not given: an unstubbed request is recorded and answered as `success:false`,
 * which the caller asserts is empty. A page that starts asking for an endpoint
 * no fixture models therefore reddens instead of silently rendering blanks.
 */

/** Response body of the enveloped endpoints (opendata/api/schemas.py:27-32). */
export function envelope<T>(data: T): Record<string, unknown> {
  return { success: true, message: 'success', data }
}

/**
 * Seed the session a successful login leaves behind.
 *
 * pinia-plugin-persistedstate stores the `auth` store's user/accessToken/
 * refreshToken under the localStorage key `auth`, and the router guard only reads
 * `isAuthenticated` (user && accessToken) — so a seeded session needs no backend.
 * It is a session-shaped input, nothing more: which pages then show *data* is
 * decided by `stubApi`, and no test may claim a rendered value came from this.
 */
export async function seedSession(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      'auth',
      JSON.stringify({
        user: {
          id: 1,
          email: 'e2e@example.com',
          username: 'e2e',
          role: 'admin',
          is_active: true,
        },
        accessToken: 'e2e-access-token',
        refreshToken: 'e2e-refresh-token',
      })
    )
  })
}

export interface Plane {
  /** `METHOD pathname` for every request the fixture answered, in arrival order. */
  served: () => string[]
  /** `METHOD pathname` for every request no fixture models — must stay empty. */
  unstubbed: () => string[]
  /** Parsed JSON bodies the page sent for `key` — what the app really submitted. */
  postBodies: (key: string) => unknown[]
}

/**
 * Serve `routes` for all `/api/v1/**` requests and record what was asked.
 *
 * Keys are `GET /api/v1/tables/` style, path only: the query string is dropped
 * so `/tables/?page=2` and `/tables/` are the same contract, and `/tables/`
 * cannot swallow `/tables/3201/schema`. Values are the whole response body.
 */
export async function stubApi(page: Page, routes: Record<string, unknown>): Promise<Plane> {
  const served: string[] = []
  const unstubbed: string[] = []
  const bodies = new Map<string, unknown[]>()
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const key = `${request.method()} ${new URL(request.url()).pathname}`
    if (!(key in routes)) {
      unstubbed.push(key)
      await route.fulfill({
        status: 599,
        contentType: 'application/json',
        body: JSON.stringify({
          success: false,
          message: `e2e fixture has no stub for ${key}`,
          error_code: 'E2E_UNSTUBBED',
        }),
      })
      return
    }
    served.push(key)
    const sent = request.postDataJSON()
    if (sent !== null && sent !== undefined) {
      bodies.set(key, [...(bodies.get(key) ?? []), sent])
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(routes[key]),
    })
  })
  return {
    served: () => [...served],
    unstubbed: () => [...unstubbed],
    postBodies: (key: string) => [...(bodies.get(key) ?? [])],
  }
}

const ISO = '2026-09-25T02:00:00'

/** One `DataScript.to_dict()` row (opendata/models/data_script.py:95-116). */
function script(scriptId: string, name: string, category: string): Record<string, unknown> {
  return {
    id: scriptId === 'c32_alpha' ? 901 : 902,
    script_id: scriptId,
    script_name: name,
    category,
    sub_category: null,
    frequency: 'daily',
    description: `desc of ${name}`,
    source: 'ths',
    target_table: 'dwd_stock_daily',
    module_path: 'akshare.stock',
    function_name: 'stock_daily',
    estimated_duration: 120,
    timeout: 600,
    is_active: true,
    is_custom: false,
    parameters: { symbol: '000001' },
    created_at: ISO,
    updated_at: ISO,
  }
}

/** What `/scripts` reads: the page plus the category chips (scripts.py:62-71, :115). */
export function scriptsPlane(): Record<string, unknown> {
  return {
    'GET /api/v1/scripts/': envelope({
      items: [
        script('c32_alpha', 'C32SCRIPT_ALPHA 日线', '股票数据'),
        script('c32_beta', 'C32SCRIPT_BETA 财务', '财务数据'),
      ],
      total: 2,
      page: 1,
      page_size: 20,
    }),
    // data is a bare list[str] here (script_service.py:259-262), not an object.
    'GET /api/v1/scripts/categories': envelope(['C32CAT_ALPHA', '股票数据', '财务数据']),
  }
}

/** What `/tables` reads: the registry page plus the ods/dwd layer view. */
export function tablesPlane(): Record<string, unknown> {
  return {
    // tables.py:265-275 — the only list endpoint that also answers total_pages.
    'GET /api/v1/tables/': envelope({
      items: [
        {
          id: 3201,
          table_name: 'C32TABLE_ALPHA',
          table_comment: 'c32 fixture table',
          category: '股票数据',
          script_id: 'c32_alpha',
          row_count: 12345,
          last_update_time: ISO,
          last_update_status: 'success',
          data_start_date: '2026-01-05',
          data_end_date: '2026-09-25',
          created_at: ISO,
          updated_at: ISO,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
      total_pages: 1,
    }),
    // tables.py:153-162 — `source` is null for every dwd row.
    'GET /api/v1/tables/warehouse': envelope({
      count: 2,
      tables: [
        {
          table: 'ods_c32_alpha',
          layer: 'ods',
          domain: 'stock_daily',
          source: 'ths',
          rows: 2000,
          size_mb: 1.5,
        },
        {
          table: 'dwd_c32_alpha',
          layer: 'dwd',
          domain: 'stock_daily',
          source: null,
          rows: 1800,
          size_mb: 1.2,
        },
      ],
    }),
  }
}

/** What `/tables/3201` reads: bare schema on load, enveloped preview on tab click. */
export function tableDetailPlane(): Record<string, unknown> {
  return {
    // tables.py:348-353 returns TableSchemaResponse directly — NO envelope.
    'GET /api/v1/tables/3201/schema': {
      table_name: 'C32TABLE_ALPHA',
      columns: [
        { name: 'C32COL_TRADE_DATE', type: 'varchar(10)', nullable: false, key: 'PRI', default: null },
        { name: 'C32COL_CLOSE', type: 'decimal(10,4)', nullable: true, key: null, default: null },
      ],
      row_count: 12345,
      last_update_time: ISO,
    },
    // tables.py:397-408 — `columns` is a list of names, `rows` a list of objects.
    'GET /api/v1/tables/3201/data': envelope({
      table_name: 'C32TABLE_ALPHA',
      columns: ['C32COL_TRADE_DATE', 'C32COL_CLOSE'],
      rows: [{ C32COL_TRADE_DATE: '2026-09-25', C32COL_CLOSE: '12.3400' }],
      row_count: 1,
      offset: 0,
      limit: 100,
    }),
  }
}

/** What `/executions` reads: list, stats card, and the failed-shard panel. */
export function executionsPlane(): Record<string, unknown> {
  return {
    // models/task.py:261-278 — rows_before/rows_after, and no rows_processed
    // (that key only exists on the download-progress payload, data.py:205).
    'GET /api/v1/executions/': envelope({
      items: [
        {
          id: 7101,
          execution_id: 'c32-exec-ok',
          task_id: 71,
          script_id: 'c32_alpha',
          status: 'completed',
          start_time: ISO,
          end_time: '2026-09-25T02:00:12',
          duration: 12.5,
          rows_before: 11000,
          rows_after: 12345,
          error_message: null,
          retry_count: 0,
          triggered_by: 'scheduler',
          created_at: ISO,
        },
        {
          id: 7102,
          execution_id: 'c32-exec-bad',
          task_id: 72,
          script_id: 'c32_beta',
          status: 'failed',
          start_time: ISO,
          end_time: '2026-09-25T02:10:00',
          duration: 3.25,
          rows_before: null,
          rows_after: null,
          error_message: 'C32ERR_UPSTREAM_REFUSED',
          retry_count: 2,
          triggered_by: 'manual',
          created_at: ISO,
        },
      ],
      total: 2,
      page: 1,
      page_size: 20,
    }),
    // execution_service.py:255-262 — success_rate is ALREADY a percentage.
    'GET /api/v1/executions/stats': envelope({
      total_count: 7,
      success_count: 6,
      failed_count: 1,
      success_rate: 85.7,
      avg_duration: 9.4,
      today_executions: 3,
    }),
    // pipeline.py:298-311 — `window` is a nested {start,end} object.
    'GET /api/v1/pipeline/failures': envelope({
      count: 1,
      failures: [
        {
          pipeline_id: 'C32PIPE_LINE_1',
          domain: 'stock_daily',
          source: 'ths',
          shard: 3,
          window: { start: '2026-09-20', end: '2026-09-24' },
          error: 'C32FAIL_SHARD_TIMEOUT',
        },
      ],
    }),
  }
}

/** What `/tasks` reads (it also lists scripts for the picker) and what create answers. */
export function tasksPlane(): Record<string, unknown> {
  const task = {
    // models/task.py:137-161 — ScheduledTask.to_dict(script_name=…).
    id: 71,
    name: 'C32TASK_ALPHA',
    description: 'c32 fixture task',
    user_id: 1,
    script_id: 'c32_alpha',
    script_name: 'C32SCRIPT_ALPHA 日线',
    schedule_type: 'daily',
    schedule_expression: '0 0 * * *',
    parameters: { symbol: '000001' },
    is_active: true,
    retry_on_failure: true,
    max_retries: 3,
    timeout: 600,
    last_execution_at: ISO,
    next_execution_at: '2026-09-26T00:00:00',
    created_at: ISO,
    updated_at: ISO,
  }
  return {
    'GET /api/v1/tasks/': envelope({ items: [task], total: 1, page: 1, page_size: 20 }),
    'GET /api/v1/scripts/': scriptsPlane()['GET /api/v1/scripts/'],
    // tasks.py:250-261 really INSERTs into scheduled_tasks — the e2e plane never
    // lets this reach a live backend, it is answered by this stub.
    'POST /api/v1/tasks/': envelope({ ...task, id: 72, name: 'C32TASK_CREATED' }),
  }
}

/**
 * What `/data` reads: the domain catalog with its five readings
 * (data_query.py::data_catalog, AC-18|02). One row is a domain: coverage and
 * range come from the merged table, freshness from the calendar baseline,
 * and each source leg carries its own last delivery.
 */
export function catalogPlane(): Record<string, unknown> {
  return {
    'GET /api/v1/data/catalog': envelope({
      domains: [
        {
          domain: 'c32_stock_daily',
          asset_class: 'stock',
          display_name: 'C32DOM_ALPHA A股日线',
          layer: 'dwd',
          table: 'dwd_c32_stock_daily',
          freshness_field: 'trade_date',
          latest: '2026-09-25',
          lag_days: 1,
          status: 'stale',
          coverage: {
            rows: 12345,
            symbols: 543,
            start: '2019-01-02',
            end: '2026-09-25',
            diff_flagged: 2,
          },
          quality: { diff_flagged: 2, diff_report_rows: 7, flag: 'flagged' },
          sources: [
            {
              source: 'ths',
              verified: true,
              table: 'ods_c32_stock_daily_ths',
              status: 'fresh',
              reason: null,
              latest: '2026-09-26',
              lag_days: 0,
            },
            {
              source: 'sina',
              verified: false,
              table: 'ods_c32_stock_daily_sina',
              status: 'stale',
              reason: null,
              latest: '2026-09-20',
              lag_days: 6,
            },
          ],
        },
        {
          domain: 'c32_index_daily',
          asset_class: 'index',
          display_name: 'C32DOM_BETA 指数日线',
          layer: 'dwd',
          table: 'dwd_c32_index_daily',
          freshness_field: 'trade_date',
          latest: null,
          lag_days: null,
          status: 'missing',
          coverage: { rows: 0, symbols: null, start: null, end: null, diff_flagged: null },
          quality: { diff_flagged: null, diff_report_rows: null, flag: 'unmeasured' },
          sources: [
            {
              source: 'sina',
              verified: false,
              table: 'ods_c32_index_daily_sina',
              status: 'unmapped',
              reason: "mapping 'sina'/'c32_index_daily' has no field 'trade_date'",
              latest: null,
              lag_days: null,
            },
          ],
        },
      ],
      expected_data_date: '2026-09-26',
      domains_total: 2,
      source_legs_total: 3,
    }),
  }
}
