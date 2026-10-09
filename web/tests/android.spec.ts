import { expect, test } from '@playwright/test'
import { signIn } from './support/helpers'

// «Скачать для Android» (M62-T1): the server offers its APK; the e2e server serves a stand-in.
test('the Android app is offered in «Ещё» with its version and size', async ({ page }) => {
  await signIn(page)
  await page.goto('/more')
  const link = page.getByTestId('android-download')
  await expect(link).toContainText('Скачать для Android')
  await expect(link).toContainText('Версия 0.1.0-e2e · 2 МБ')
  expect(await link.getAttribute('href')).toBe('/downloads/mia.apk')
  const download = await page.request.get('/downloads/mia.apk')
  expect(download.status()).toBe(200)
  expect(download.headers()['content-type']).toBe('application/vnd.android.package-archive')
  expect(download.headers()['content-disposition']).toContain('attachment')
})

test('the sign-in screen links the Android app too', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByTestId('android-download')).toBeVisible()
})
