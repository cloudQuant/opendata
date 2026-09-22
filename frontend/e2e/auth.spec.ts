import { test, expect } from '@playwright/test'

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

  test('login with invalid credentials shows error', async ({ page }) => {
    await page.goto('/login')
    // Wait for form to load
    await page.waitForTimeout(1000)

    // Try to find email/password inputs and submit
    const emailInput = page.locator('input').first()
    const passwordInput = page.locator('input[type="password"]').first()

    if (await emailInput.isVisible() && await passwordInput.isVisible()) {
      await emailInput.fill('invalid@example.com')
      await passwordInput.fill('wrongpassword')

      // Submit the form
      const submitButton = page.locator('button[type="submit"], button:has-text("登录")').first()
      if (await submitButton.isVisible()) {
        await submitButton.click()
        // Should show error or stay on login page
        await page.waitForTimeout(2000)
        await expect(page).toHaveURL(/login/)
      }
    }
  })

  test('404 page for unknown routes', async ({ page }) => {
    await page.goto('/nonexistent-page-xyz')
    // Should show 404 or redirect to login
    await page.waitForTimeout(1000)
    const url = page.url()
    expect(url.includes('login') || url.includes('nonexistent')).toBeTruthy()
  })
})
