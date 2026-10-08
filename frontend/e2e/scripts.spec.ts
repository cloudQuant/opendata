import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import {
  catalogPlane,
  executionsPlane,
  scriptsPlane,
  seedSession,
  scriptDetailPlane,
  stubApi,
  tableDetailPlane,
  tablesPlane,
} from './fixtures'

/**
 * The authenticated catalog, legacy script list, /tables and /executions pages.
 *
 * These leaves used to be `test.skip` with a TODO for a login helper (C30 named
 * all seven). The helper was never the blocker: what was missing was a per-endpoint
 * response fixture, measured rather than invented — see fixtures.ts for why a
 * generic `{items, total}` stub renders three of these four pages only.
 */
test.describe('Scripts & Data Tables E2E', () => {
  test('scripts page requires authentication', async ({ page }) => {
    await page.goto('/scripts')
    await expect(page).toHaveURL(/login/)
  })

  test('tables page requires authentication', async ({ page }) => {
    await page.goto('/tables')
    await expect(page).toHaveURL(/login/)
  })

  test('executions page requires authentication', async ({ page }) => {
    await page.goto('/executions')
    await expect(page).toHaveURL(/login/)
  })

  test.describe('Authenticated', () => {
    test('legacy script list shows real DataScript identities', async ({ page }) => {
      const plane = await stubApi(page, scriptsPlane())
      await seedSession(page)
      await page.goto('/scripts/functions')

      // The list itself, the total from the same reply, and the category chips
      // from a second endpoint whose `data` is a bare list[str].
      await expect(page.getByText('C32SCRIPT_ALPHA 日线', { exact: true })).toBeVisible()
      await expect(page.getByText('共 2 个接口')).toBeVisible()
      await expect(page.getByText('C32CAT_ALPHA')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
    })

    test('merged catalog is the default at both catalog URLs and filters by market and dataset', async ({
      page,
    }) => {
      const plane = await stubApi(page, catalogPlane())
      const catalogDocumentRequests: string[] = []
      page.on('request', (request) => {
        if (request.resourceType() !== 'document') return
        const pathname = new URL(request.url()).pathname
        if (pathname === '/scripts' || pathname === '/data') {
          catalogDocumentRequests.push(pathname)
        }
      })
      await seedSession(page)
      await page.goto('/scripts')

      await expect(page.getByRole('heading', { name: '数据目录' })).toBeVisible()
      await page.waitForLoadState('networkidle')
      // A cold Vite reload is another document load; count those actual requests,
      // not Vue Router's same-path history events.
      expect(catalogDocumentRequests).toContain('/scripts')
      const requestsBeforeLocalFilters = plane.served()
      const documentsBeforeLocalFilters = [...catalogDocumentRequests]
      expect(requestsBeforeLocalFilters).toEqual(
        documentsBeforeLocalFilters.map(() => 'GET /api/v1/data/catalog')
      )
      await expect(page.getByRole('heading', { name: /市场：cn/ })).toBeVisible()
      await expect(page.getByRole('heading', { name: /市场：eu/ })).toBeVisible()
      await expect(page.getByRole('heading', { name: /市场：global/ })).toBeVisible()
      await expect(page.getByText('C32DOM_ALPHA A股日线', { exact: true })).toBeVisible()
      // This domain has both eu and global capabilities, so it appears in both
      // actual-market groups in the merged catalogue.
      await expect(page.getByText('C32DOM_BETA 指数日线', { exact: true })).toHaveCount(2)
      await expect(page.getByText(/覆盖、时间范围、新鲜度和质量按合并数据域统计/)).toBeVisible()

      await page.getByTestId('registered-functions').first().click()
      await expect(page.getByText('ThsStockDailyFetcher.fetch')).toBeVisible()
      await expect(page.getByText('GET /api/v1/data/stock/c32_stock_daily')).toHaveCount(2)
      await expect(page.getByText('symbol: str（必填）')).toBeVisible()
      await page.getByRole('button', { name: '关闭' }).last().click()

      await page.getByTestId('market-filter').click()
      await page.getByRole('option', { name: 'eu', exact: true }).click()
      await expect(page.getByRole('heading', { name: /市场：eu/ })).toBeVisible()
      await expect(page.getByText('C32DOM_BETA 指数日线', { exact: true })).toBeVisible()
      await expect(page.getByText('C32DOM_ALPHA A股日线', { exact: true })).toHaveCount(0)

      await page.getByTestId('domain-filter').click()
      await page.getByRole('option', { name: 'C32DOM_BETA 指数日线 (c32_index_daily)' }).click()
      await expect(page.getByText('C32DOM_BETA 指数日线', { exact: true })).toBeVisible()
      await expect(page.getByText('C32DOM_ALPHA A股日线', { exact: true })).toHaveCount(0)
      expect(plane.served()).toEqual(requestsBeforeLocalFilters)
      expect(catalogDocumentRequests).toEqual(documentsBeforeLocalFilters)

      await page.goto('/data')
      await expect(page.getByRole('heading', { name: '数据目录' })).toBeVisible()
      await page.waitForLoadState('networkidle')
      expect(catalogDocumentRequests).toContain('/data')
      expect(plane.served().slice(requestsBeforeLocalFilters.length)).toEqual([
        'GET /api/v1/data/catalog',
      ])
      expect(plane.served()).toEqual(
        catalogDocumentRequests.map(() => 'GET /api/v1/data/catalog')
      )
      expect(plane.unstubbed()).toEqual([])
    })

    test('catalog function drilldown reaches legacy list and an actual script detail route', async ({
      page,
    }) => {
      const plane = await stubApi(page, {
        ...catalogPlane(),
        ...scriptsPlane(),
        ...scriptDetailPlane(),
      })
      await seedSession(page)
      await page.goto('/scripts')

      await page.getByRole('link', { name: '查看旧版脚本函数列表' }).click()
      await expect(page).toHaveURL(/\/scripts\/functions$/)
      await expect(page.getByText('此处是旧版 DataScript 函数目录。')).toBeVisible()
      await expect(page.getByText('C32SCRIPT_ALPHA 日线', { exact: true }).first()).toBeVisible()

      await page.getByRole('button', { name: '查看详情' }).first().click()
      await expect(page).toHaveURL(/\/scripts\/c32_alpha$/)
      await expect(page.getByText('C32SCRIPT_ALPHA 日线', { exact: true }).first()).toBeVisible()
      await expect(page.getByText('akshare.stock')).toBeVisible()
      expect(plane.served()).toContain('GET /api/v1/scripts/c32_alpha')
      expect(plane.unstubbed()).toEqual([])
    })

    test('catalog renders an API error and an empty payload visibly', async ({ page }) => {
      await seedSession(page)
      await page.route('**/api/v1/data/catalog', async (route) => {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ success: false, message: 'catalog unavailable' }),
        })
      })
      await page.goto('/scripts')
      await expect(page.locator('.el-alert--error')).toBeVisible()

      await page.unroute('**/api/v1/data/catalog')
      await stubApi(page, {
        'GET /api/v1/data/catalog': {
          success: true,
          message: 'success',
          data: {
            domains: [],
            markets: [],
            expected_data_date: '',
            domains_total: 0,
            source_legs_total: 0,
          },
        },
      })
      await page.reload()
      await expect(page.getByText('暂无数据域')).toBeVisible()
    })

    test('tables list shows data and the warehouse layers', async ({ page }) => {
      const plane = await stubApi(page, tablesPlane())
      await seedSession(page)
      await page.goto('/tables')

      await expect(page.getByText('C32TABLE_ALPHA')).toBeVisible()
      await expect(page.getByText('共 1 个表')).toBeVisible()
      // The ods/dwd card reads /tables/warehouse, whose `source` is null for dwd
      // rows and renders as '—' — so both layers must show up, with the null
      // distinct from a blank cell.
      await expect(page.getByText('ods_c32_alpha')).toBeVisible()
      await expect(page.getByText('dwd_c32_alpha')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
    })

    test('table detail shows schema and preview', async ({ page }) => {
      const plane = await stubApi(page, { ...tablesPlane(), ...tableDetailPlane() })
      await seedSession(page)
      await page.goto('/tables')

      // Clicking a table row navigates nothing (TablesView has no row-click
      // handler — C32 measured it, the retired version of this test assumed it
      // did). The only wiring to the detail page is this button.
      await page.getByRole('button', { name: '查看详情' }).click()
      await expect(page).toHaveURL(/\/tables\/3201$/)

      // /tables/3201/schema answers a BARE object — wrapped in the envelope this
      // page would read `{success,…}.columns` and show an empty table.
      await expect(page.getByText('C32COL_TRADE_DATE')).toBeVisible()
      await expect(page.getByText('varchar(10)')).toBeVisible()

      // The preview is lazy: /tables/3201/data is requested only when the tab is
      // opened (TableDetailView.vue:42), so this click is part of the contract.
      await page.getByRole('tab', { name: '预览数据' }).click()
      await expect(page.getByText('12.3400')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
    })

    test('executions list shows history and the failed-shard panel', async ({ page }) => {
      const plane = await stubApi(page, executionsPlane())
      await seedSession(page)
      await page.goto('/executions')

      // Three endpoints, three panels: stats cards, the failure panel, the table.
      await expect(page.getByText('C32PIPE_LINE_1')).toBeVisible()
      await expect(page.getByText('C32FAIL_SHARD_TIMEOUT')).toBeVisible()
      await expect(page.getByText('C32ERR_UPSTREAM_REFUSED')).toBeVisible()
      await expect(page.getByText('c32_alpha')).toBeVisible()

      // 85.7 is what the backend answers (execution_service.py:259 already
      // multiplied by 100); the card used to multiply again and read 8570.0%.
      await expect(page.getByText('85.7%', { exact: true })).toBeVisible()

      // 'completed' has to render as 成功 — the frontend word list had 'success',
      // a status the backend never writes, so the Chinese label was unreachable.
      await expect(page.getByText('成功', { exact: true })).toBeVisible()
      await expect(page.getByText('失败', { exact: true })).toBeVisible()

      // rows_after, not the rows_processed this column used to read (that key is
      // on the download-progress payload only).
      await expect(page.getByText('11000 → 12345')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
    })

    test('retry posts and reloads the failed-shard list', async ({ page }) => {
      const routes = executionsPlane()
      const failureRoute = 'GET /api/v1/pipeline/failures'
      const initialFailures = routes[failureRoute]
      let failureReads = 0
      Object.defineProperty(routes, failureRoute, {
        enumerable: true,
        get: () => {
          failureReads += 1
          return failureReads === 1
            ? initialFailures
            : {
                success: true,
                message: 'success',
                data: { count: 0, failures: [] },
              }
        },
      })
      const plane = await stubApi(page, routes)
      await seedSession(page)
      await page.goto('/executions')

      await expect(page.getByText('C32FAIL_SHARD_TIMEOUT')).toBeVisible()
      await page.getByRole('button', { name: '一键重试' }).click()

      await expect(page.getByText('已重置 1 个失败分片，下次运行将自动重试')).toBeVisible()
      await expect(page.getByText('暂无失败分片')).toBeVisible()
      expect(plane.served()).toContain('POST /api/v1/pipeline/retry-failed')
      expect(plane.served().filter((entry) => entry === failureRoute)).toHaveLength(2)
      expect(plane.unstubbed()).toEqual([])
    })

    test('failed-shard export downloads the visible JSON response', async ({ page }) => {
      const plane = await stubApi(page, executionsPlane())
      await seedSession(page)
      await page.goto('/executions')

      const [download] = await Promise.all([
        page.waitForEvent('download'),
        page.getByTestId('export-failures').click(),
      ])
      expect(download.suggestedFilename()).toBe('pipeline-failures.json')
      const filePath = await download.path()
      expect(filePath).not.toBeNull()
      const exported = JSON.parse(await readFile(filePath!, 'utf8')) as {
        count: number
        failures: Array<{ pipeline_id: string; error: string }>
      }
      expect(exported.count).toBe(1)
      expect(exported.failures[0]).toMatchObject({
        pipeline_id: 'C32PIPE_LINE_1',
        error: 'C32FAIL_SHARD_TIMEOUT',
      })
      expect(plane.unstubbed()).toEqual([])
    })

    test('empty failed-shard list is visible and exports an empty JSON array', async ({ page }) => {
      const routes = executionsPlane()
      routes['GET /api/v1/pipeline/failures'] = {
        success: true,
        message: 'success',
        data: { count: 0, failures: [] },
      }
      const plane = await stubApi(page, routes)
      await seedSession(page)
      await page.goto('/executions')

      await expect(page.getByText('暂无失败分片')).toBeVisible()
      const [download] = await Promise.all([
        page.waitForEvent('download'),
        page.getByTestId('export-failures').click(),
      ])
      const filePath = await download.path()
      expect(filePath).not.toBeNull()
      expect(JSON.parse(await readFile(filePath!, 'utf8'))).toEqual({ count: 0, failures: [] })
      expect(plane.unstubbed()).toEqual([])
    })

    test('failed-shard export errors are visible to the operator', async ({ page }) => {
      await page.addInitScript(() => {
        Object.defineProperty(URL, 'createObjectURL', {
          configurable: true,
          value: () => {
            throw new Error('download blocked')
          },
        })
      })
      const plane = await stubApi(page, executionsPlane())
      await seedSession(page)
      await page.goto('/executions')

      await expect(page.getByTestId('export-failures')).toBeEnabled()
      await page.getByTestId('export-failures').click()
      await expect(page.getByText('失败清单导出失败')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
    })

    test('failed-shard load errors are visible and disable export', async ({ page }) => {
      const routes = executionsPlane()
      routes['GET /api/v1/pipeline/failures'] = {
        success: false,
        message: 'failure list unavailable',
      }
      const plane = await stubApi(page, routes)
      await seedSession(page)
      await page.goto('/executions')

      await expect(page.locator('.el-alert--error')).toContainText('失败清单加载失败')
      await expect(page.getByTestId('export-failures')).toBeDisabled()
      expect(plane.unstubbed()).toEqual([])
    })

    test('catalog page shows a domain with its five readings', async ({ page }) => {
      const plane = await stubApi(page, catalogPlane())
      await seedSession(page)
      await page.goto('/data')

      // The C32 markers prove these readings came from the stub, not a live backend.
      await expect(page.getByText('C32DOM_ALPHA A股日线', { exact: true })).toBeVisible()
      // 新鲜度的基准：滞后天数是相对交易日历的期望日算的，基准日必须可见，
      // 否则「滞后 1 天」无法复核（AC-18|02）。
      await expect(page.getByText('基准日 2026-09-26 · 2 域 · 3 源腿')).toBeVisible()
      // 覆盖标的数 / 时间范围 / 新鲜度
      await expect(page.getByText('12345 行 · 543 标的')).toBeVisible()
      await expect(page.getByText('2019-01-02 ~ 2026-09-25')).toBeVisible()
      await expect(page.getByText('2026-09-25（滞后 1 天）')).toBeVisible()
      // 各源最近更新：验证状态属于一条腿，不属于一个域
      await expect(page.getByText('ths 2026-09-26（滞后 0 天） · 已验证')).toBeVisible()
      await expect(page.getByText('sina 2026-09-20（滞后 6 天） · 未验证')).toBeVisible()
      // 质量标记
      await expect(page.getByText('有差异', { exact: true })).toBeVisible()
      // 测不出来就照说：空表是 0 行，没有差异列是未测量，没有字段映射是未映射
      await expect(page.getByText('0 行 · —')).toHaveCount(2)
      await expect(page.getByText('未测量', { exact: true })).toHaveCount(2)
      await expect(page.getByText('sina 未映射 · 未验证')).toHaveCount(2)
      expect(plane.unstubbed()).toEqual([])
    })
  })
})
