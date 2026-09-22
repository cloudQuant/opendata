import { test, expect } from '@playwright/test'

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
    // TODO: Implement login helper
    // test.beforeEach(async ({ page }) => { await loginAsAdmin(page) })

    test.skip('scripts list shows data', async ({ page }) => {
      await page.goto('/scripts')
      await expect(page.locator('table, .el-table').first()).toBeVisible({ timeout: 10000 })
    })

    test.skip('tables list shows data', async ({ page }) => {
      await page.goto('/tables')
      await expect(page.locator('table, .el-table').first()).toBeVisible({ timeout: 10000 })
    })

    test.skip('table detail shows schema and preview', async ({ page }) => {
      await page.goto('/tables')
      // Click first table row
      const firstRow = page.locator('tr, .el-table__row').first()
      if (await firstRow.isVisible()) {
        await firstRow.click()
        // Should navigate to detail page
        await expect(page).toHaveURL(/tables\//)
      }
    })

    test.skip('executions list shows history', async ({ page }) => {
      await page.goto('/executions')
      await expect(page.locator('table, .el-table, .el-empty').first()).toBeVisible({ timeout: 10000 })
    })
  })
})
