import { test, expect } from '@playwright/test'

test.describe('Tasks Management E2E', () => {
  // TODO: Add authenticated setup fixture
  // These tests require a running backend with test data

  test('tasks page requires authentication', async ({ page }) => {
    await page.goto('/tasks')
    // Should redirect to login
    await expect(page).toHaveURL(/login/)
  })

  test.describe('Authenticated', () => {
    // TODO: Implement login helper that stores auth token
    // test.beforeEach(async ({ page }) => {
    //   await loginAsAdmin(page)
    // })

    test.skip('tasks list page loads', async ({ page }) => {
      await page.goto('/tasks')
      await expect(page).toHaveURL(/tasks/)
      // Should show tasks table or empty state
      await expect(page.locator('table, .el-empty, .el-table').first()).toBeVisible({ timeout: 10000 })
    })

    test.skip('can create a new task', async ({ page }) => {
      await page.goto('/tasks')
      // Click create button
      const createBtn = page.locator('button:has-text("创建"), button:has-text("新建")').first()
      await createBtn.click()
      // Should show create form/dialog
      await expect(page.locator('.el-dialog, .el-drawer').first()).toBeVisible()
    })

    test.skip('can trigger task execution', async ({ page }) => {
      await page.goto('/tasks')
      // Find and click trigger button on first task row
      const triggerBtn = page.locator('button:has-text("执行"), button:has-text("触发")').first()
      if (await triggerBtn.isVisible()) {
        await triggerBtn.click()
        // Should show confirmation or success message
        await expect(page.locator('.el-message, .el-notification').first()).toBeVisible({ timeout: 5000 })
      }
    })
  })
})
