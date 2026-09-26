import { test, expect } from '@playwright/test'
import {
  executionsPlane,
  seedSession,
  stubApi,
  tasksPlane,
  type Plane,
} from './fixtures'

/** The backend's own gate on this field: opendata/api/schemas.py:229. */
const SCHEDULE_TYPES = /^(once|daily|weekly|monthly|cron|interval)$/

/**
 * The three authenticated /tasks leaves retired here (C30 named them).
 *
 * One of them is renamed, and deliberately so: 'can trigger task execution'
 * described a button the frontend does not have. C32 measured the row actions
 * (TasksView.vue:220-243 — 编辑 / 执行记录 / 删除), and `button:has-text("执行")`
 * substring-matched 「执行记录」, which only navigates. Keeping the old name
 * would have meant keeping the fiction.
 */
test.describe('Tasks Management E2E', () => {
  test('tasks page requires authentication', async ({ page }) => {
    await page.goto('/tasks')
    // Should redirect to login
    await expect(page).toHaveURL(/login/)
  })

  test.describe('Authenticated', () => {
    test('tasks list page loads', async ({ page }) => {
      const plane: Plane = await stubApi(page, { ...tasksPlane(), ...executionsPlane() })
      await seedSession(page)
      await page.goto('/tasks')

      await expect(page.getByText('C32TASK_ALPHA')).toBeVisible()
      await expect(page.getByText('0 0 * * *')).toBeVisible()
      // The create dialog's script picker is fed on mount, which is why this page
      // asks for /scripts/ too (measured: docs/evidence/C32/frontend-request-census.txt).
      expect(plane.served()).toContain('GET /api/v1/scripts/')
      expect(plane.unstubbed()).toEqual([])
    })

    test('can create a new task', async ({ page }) => {
      const plane = await stubApi(page, tasksPlane())
      await seedSession(page)
      await page.goto('/tasks')

      await page.getByRole('button', { name: '创建任务' }).click()
      const dialog = page.getByRole('dialog')
      await expect(dialog).toBeVisible()
      await dialog.getByPlaceholder('请输入任务名称').fill('C32TASK_CREATED')

      // Element Plus 2.13 renders the select's placeholder as a span, not an
      // input attribute, so the form item's label is the stable handle.
      await dialog.locator('.el-form-item', { hasText: '数据接口' }).locator('.el-select').click()
      await page.getByRole('option', { name: 'C32SCRIPT_ALPHA 日线' }).click()

      // The defect this click retires: 每小时 used to submit schedule_type
      // 'hourly', which the backend pattern rejects, so the button could never
      // create anything. The assertion below is the contract, not the toast.
      await dialog.locator('.el-form-item', { hasText: '调度类型' }).locator('.el-select').click()
      await page.getByRole('option', { name: '每小时' }).click()

      await dialog.getByRole('button', { name: '创建', exact: true }).click()
      await expect(page.locator('.el-message').first()).toContainText('创建成功')

      const [submitted] = plane.postBodies('POST /api/v1/tasks/')
      expect(submitted).toMatchObject({
        name: 'C32TASK_CREATED',
        script_id: 'c32_alpha',
        schedule_type: 'cron',
        schedule_expression: '0 * * * *',
      })
      expect(String((submitted as { schedule_type?: string }).schedule_type)).toMatch(
        SCHEDULE_TYPES,
      )
      expect(plane.unstubbed()).toEqual([])
    })

    test('the 执行记录 action opens that task’s executions', async ({ page }) => {
      const plane = await stubApi(page, { ...tasksPlane(), ...executionsPlane() })
      await seedSession(page)
      await page.goto('/tasks')

      await page.getByRole('button', { name: '执行记录' }).click()
      // The one thing the row action really does: navigate with the task id kept.
      await expect(page).toHaveURL('/executions?task_id=71')
      await expect(page.getByText('C32PIPE_LINE_1')).toBeVisible()
      expect(plane.unstubbed()).toEqual([])
    })
  })
})
