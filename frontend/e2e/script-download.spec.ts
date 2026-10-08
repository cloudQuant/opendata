import { expect, test, type Page, type Route } from '@playwright/test'
import { seedSession } from './fixtures'

const SCRIPT = {
  id: 7,
  script_id: 'legacy-script',
  script_name: 'C32_SCRIPT_DOC_LEGACY',
  category: 'C32_CAT_MARKET',
  sub_category: null,
  frequency: 'daily',
  description: 'C32_SCRIPT_DOCUMENTATION',
  source: 'ths',
  target_table: null,
  module_path: 'legacy.module',
  function_name: 'download',
  parameters: [],
  estimated_duration: 10,
  timeout: 60,
  is_active: true,
  is_custom: false,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function envelope(data: unknown) {
  return { success: true, message: 'success', data }
}

interface DownloadPlane {
  requestBodies: unknown[]
  interfaceQueries: Array<Record<string, string>>
  unexpected: string[]
}

async function installApiStubs(page: Page, downloadStatus: number): Promise<DownloadPlane> {
  const plane: DownloadPlane = {
    requestBodies: [],
    interfaceQueries: [],
    unexpected: [],
  }

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const key = `${request.method()} ${url.pathname}`

    if (key === 'GET /api/v1/scripts/legacy-script') {
      await route.fulfill({ status: 200, json: envelope(SCRIPT) })
      return
    }

    if (key === 'GET /api/v1/data/interfaces/') {
      plane.interfaceQueries.push(Object.fromEntries(url.searchParams.entries()))
      await route.fulfill({
        status: 200,
        json: envelope({
          items: [
            {
              id: 7,
              name: 'same_numeric_id',
              display_name: 'C32_INTERFACE_ID_COLLISION',
              description: 'C32_DESCRIPTION_COLLISION',
              category_name: 'C32_CAT_MARKET',
              is_active: true,
            },
            {
              id: 91,
              name: 'actual_market_data',
              display_name: 'C32_INTERFACE_SELECTED_91',
              description: 'C32_DESCRIPTION_SELECTED_91',
              category_name: 'C32_CAT_MARKET',
              is_active: true,
            },
          ],
          total: 2,
          page: 1,
          page_size: 100,
          total_pages: 1,
        }),
      })
      return
    }

    if (key === 'POST /api/v1/data/download') {
      plane.requestBodies.push(request.postDataJSON())
      if (downloadStatus === 202) {
        await route.fulfill({
          status: 202,
          json: { execution_id: 8401, status: 'pending', message: 'C32_DOWNLOAD_QUEUED' },
        })
      } else {
        await route.fulfill({
          status: downloadStatus,
          json: { detail: 'C32_DOWNLOAD_422_REJECTED' },
        })
      }
      return
    }

    if (key === 'GET /api/v1/executions/') {
      await route.fulfill({
        status: 200,
        json: envelope({ items: [], total: 0, page: 1, page_size: 20, total_pages: 0 }),
      })
      return
    }

    if (key === 'GET /api/v1/executions/stats') {
      await route.fulfill({
        status: 200,
        json: envelope({
          total_count: 0,
          success_count: 0,
          failed_count: 0,
          success_rate: 0,
          avg_duration: 0,
          today_executions: 0,
        }),
      })
      return
    }

    if (key === 'GET /api/v1/pipeline/failures') {
      await route.fulfill({ status: 200, json: envelope({ count: 0, failures: [] }) })
      return
    }

    plane.unexpected.push(key)
    await route.fulfill({
      status: 599,
      json: { success: false, message: `C32_UNEXPECTED_API_${key}` },
    })
  })

  return plane
}

async function chooseRealInterface(page: Page) {
  const selector = page.getByTestId('download-interface-select')
  await expect(selector).toBeVisible()
  await selector.click()
  await page.getByText('C32_INTERFACE_SELECTED_91', { exact: true }).click()
  await expect(page.getByTestId('download-interface-select')).toContainText(
    'C32_INTERFACE_SELECTED_91'
  )
}

test.describe('script detail download with stubbed API', () => {
  test('posts only selected interface 91 and follows the accepted task to executions', async ({
    page,
  }) => {
    const plane = await installApiStubs(page, 202)
    await seedSession(page)
    await page.goto('/scripts/legacy-script')

    await expect(page.getByText('C32_SCRIPT_DOC_LEGACY').first()).toBeVisible()
    await expect(page.getByText('C32_SCRIPT_DOCUMENTATION')).toBeVisible()
    await chooseRealInterface(page)
    await page.getByRole('button', { name: '创建下载任务' }).click()

    await expect(page).toHaveURL(/\/executions$/)
    await expect(page.locator('.executions-view')).toBeVisible()
    expect(plane.interfaceQueries).toEqual([
      { page: '1', page_size: '100', is_active: 'true' },
    ])
    expect(plane.requestBodies).toEqual([{ interface_id: 91, parameters: {} }])
    expect(plane.unexpected).toEqual([])
  })

  test('keeps the user on script details and shows no success after a 422', async ({ page }) => {
    const plane = await installApiStubs(page, 422)
    await seedSession(page)
    await page.goto('/scripts/legacy-script')
    await chooseRealInterface(page)
    await page.getByRole('button', { name: '创建下载任务' }).click()

    await expect(page).toHaveURL(/\/scripts\/legacy-script$/)
    await expect(page.getByText('C32_DOWNLOAD_422_REJECTED').first()).toBeVisible()
    await expect(page.locator('.el-message--success')).toHaveCount(0)
    expect(plane.requestBodies).toEqual([{ interface_id: 91, parameters: {} }])
    expect(plane.unexpected).toEqual([])
  })
})
