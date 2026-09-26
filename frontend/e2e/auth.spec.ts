import { test, expect, type Page } from '@playwright/test'

/**
 * Seed the session a successful login would have left behind.
 *
 * pinia-plugin-persistedstate stores the `auth` store's user/accessToken/
 * refreshToken under the localStorage key `auth`, and the router guard only
 * reads `isAuthenticated` (user && accessToken) — so a seeded session needs
 * no backend and no mock. It is a session-shaped input, nothing more: no test
 * here claims a page rendered *data* from it.
 */
async function seedSession(page: Page) {
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

test.describe('Authentication E2E', () => {
  test('login page loads', async ({ page }) => {
    await page.goto('/login')
    await expect(page).toHaveURL(/login/)
    // Check for login form elements
    await expect(page.locator('input[type="text"], input[type="email"], input[placeholder*="邮箱"], input[placeholder*="email"]').first()).toBeVisible({ timeout: 10000 })
  })

  test('register page loads', async ({ page }) => {
    await page.goto('/register')
    await expect(page).toHaveURL(/register/)
  })

  test('unauthenticated user redirected to login', async ({ page }) => {
    await page.goto('/')
    // Should redirect to login page
    await expect(page).toHaveURL(/login/)
  })

  // Measured 2026-09-26 (docs/evidence/C30/login-reaction-probe.txt): a
  // rejected login renders its error toast and then the 401 branch of
  // src/utils/request.ts sets window.location.href = '/login', which reloads
  // the page the toast is on — so "the user can read the error" is not a
  // stable claim on the shipped app, and asserting it would flake. What this
  // test does pin is the two things that hold every time.
  test('login with invalid credentials submits and leaves no session', async ({ page }) => {
    const attempts: Array<Record<string, unknown>> = []
    await page.route('**/api/v1/auth/login', async (route) => {
      attempts.push(route.request().postDataJSON() as Record<string, unknown>)
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Incorrect email or password' }),
      })
    })

    await page.goto('/login')
    await page.getByPlaceholder('请输入邮箱').fill('invalid@example.com')
    await page.getByPlaceholder('请输入密码').fill('wrongpassword')
    await page.getByRole('button', { name: '登录' }).click()

    // The form really submits what was typed: a broken v-model binding or a
    // button that stopped calling the store reddens this.
    await expect.poll(() => attempts.at(-1)).toEqual({
      email: 'invalid@example.com',
      password: 'wrongpassword',
    })

    // A rejected login really leaves the visitor unsigned-in: if the store
    // persisted a token on the error path, the guard would let them in.
    // toPass() rather than poll() because the 401 branch reloads the page, and
    // an evaluate that lands mid-navigation throws "execution context was
    // destroyed" — which poll() reports instead of retrying.
    await expect(async () => {
      const stored = await page.evaluate(() => window.localStorage.getItem('auth'))
      const token = stored
        ? (JSON.parse(stored) as { accessToken?: string | null }).accessToken ?? null
        : null
      expect(token).toBeNull()
    }).toPass({ timeout: 10_000 })
  })

  test('unknown routes send an anonymous user to login with the target kept', async ({ page }) => {
    await page.goto('/nonexistent-page-xyz')
    await expect(page).toHaveURL(/\/login/)
    // The guard's other half: the path is handed to the login page so a
    // successful login can continue there.
    const redirect = await page.evaluate(() => {
      return new URL(window.location.href).searchParams.get('redirect')
    })
    expect(redirect).toBe('/nonexistent-page-xyz')
  })

  test('404 page renders for a signed-in user', async ({ page }) => {
    await seedSession(page)
    await page.goto('/nonexistent-page-xyz')
    // Not bounced to login: the guard only stops unauthenticated visitors.
    await expect(page).toHaveURL(/nonexistent-page-xyz$/)
    await expect(page.getByRole('heading', { name: '404' })).toBeVisible()
    await expect(page.getByText('抱歉，您访问的页面不存在')).toBeVisible()
  })
})
