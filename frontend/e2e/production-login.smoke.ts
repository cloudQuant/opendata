import { expect, test } from '@playwright/test'

const productionOrigin = 'http://127.0.0.1:33567'

test('production login page renders its form without browser errors', async ({ page }) => {
  const pageErrors: string[] = []
  const apiRequests: string[] = []

  page.on('pageerror', (error) => pageErrors.push(`${error.name}: ${error.message}`))
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.origin !== productionOrigin || url.pathname.startsWith('/api/')) {
      apiRequests.push(request.url())
    }
  })
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== productionOrigin || url.pathname.startsWith('/api/')) {
      await route.abort()
      return
    }
    await route.continue()
  })

  await page.goto('/login', { waitUntil: 'networkidle' })

  expect(pageErrors, 'production bundle should not raise page errors').toEqual([])
  await expect(page.locator('input[type="email"]')).toBeVisible()
  await expect(page.locator('input[type="password"]')).toBeVisible()
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible()
  expect(apiRequests, 'smoke must stay on the local production preview').toEqual([])
})
