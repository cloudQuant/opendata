import { test, expect } from '@playwright/test'
import {
  catalogPlane,
  executionsPlane,
  scriptsPlane,
  seedSession,
  stubApi,
  tableDetailPlane,
  tablesPlane,
} from './fixtures'

/**
 * The four authenticated pages behind /scripts, /tables, /executions and /data.
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
    test('scripts list shows data', async ({ page }) => {
      const plane = await stubApi(page, scriptsPlane())
      await seedSession(page)
      await page.goto('/scripts')

      // The list itself, the total from the same reply, and the category chips
      // from a second endpoint whose `data` is a bare list[str].
      await expect(page.getByText('C32SCRIPT_ALPHA 日线', { exact: true })).toBeVisible()
      await expect(page.getByText('共 2 个接口')).toBeVisible()
      await expect(page.getByText('C32CAT_ALPHA')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
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

    test('catalog page lists domains with verification state', async ({ page }) => {
      const plane = await stubApi(page, catalogPlane())
      await seedSession(page)
      await page.goto('/data')

      await expect(page.getByText('C32DOM_ALPHA A股日线')).toBeVisible()
      await expect(page.getByText('已验证', { exact: true })).toBeVisible()
      await expect(page.getByText('未验证', { exact: true })).toBeVisible()
      await expect(page.getByText('滞后 1 天')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
    })
  })
})
