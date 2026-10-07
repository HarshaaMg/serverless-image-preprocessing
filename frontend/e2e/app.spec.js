// Real-browser tests of the full workflow against the LOCAL stack (mock AWS).
// (They were also run against the real AWS backend; see playwright.config.js.)
// Each test opens the app, interacts like a user (choose file, tick features, click),
// and checks what is shown on the page and what gets downloaded.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const FIXTURES = path.join(path.dirname(fileURLToPath(import.meta.url)), '.fixtures')
const fixture = (name) => path.join(FIXTURES, name)
const JOB_STATUS_URL = /\/api\/jobs\/[0-9a-f-]{36}$/

function feature(page, title) {
  return page.locator('.feature').filter({ has: page.locator('strong', { hasText: new RegExp(`^${title}$`) }) })
}

async function choose(page, file) {
  await page.locator('input[type=file]').setInputFiles(fixture(file))
}

async function enable(page, ...titles) {
  for (const t of titles) await feature(page, t).locator('.feature-head input').check()
}

function resultsResponse(page) {
  return page.waitForResponse((r) => r.url().endsWith('/results') && r.request().method() === 'GET', { timeout: 90_000 })
}

function card(page, label) {
  return page.locator('.result').filter({ has: page.locator('h3', { hasText: label }) })
}

// The main happy path: every operation, every preview, every download, then reset.
test('JPEG: all four operations, previews, downloads, process another', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Supported formats:')).toContainText('JPG, JPEG, PNG')
  await expect(page.getByText('Maximum file size:')).toContainText('10 MB')

  await choose(page, 'photo.jpg')
  await expect(page.locator('.selected-file')).toContainText('1600 × 1000 px')

  await enable(page, 'Resize', 'Grayscale', 'Compression', 'Pixelation')
  await feature(page, 'Compression').locator('input[type=range]').fill('40')
  await feature(page, 'Pixelation').locator('input[type=range]').fill('32')
  await expect(feature(page, 'Pixelation')).toContainText('Pixelation level: 32')

  const resultsPromise = resultsResponse(page)
  await page.getByRole('button', { name: 'Process Image' }).click()
  await expect(page.locator('.stages li.active, .stages li.done').first()).toBeVisible()
  const results = await (await resultsPromise).json()

  await expect(page.getByRole('heading', { name: /Results and Preview/ })).toBeVisible()
  await expect(page.locator('.badge')).toHaveText('COMPLETED')
  await expect(page.locator('.result')).toHaveCount(6) // original + 4 single + combined

  await expect(card(page, 'Resized')).toContainText('600 × 375 px')
  await expect(card(page, 'Grayscale')).toContainText('1600 × 1000 px')
  await expect(card(page, 'Compressed')).toContainText('JPEG quality 40')
  await expect(card(page, 'Compressed')).toContainText('% smaller')
  await expect(card(page, 'Pixelated')).toContainText('Pixelation level 32 (grid 32×20)')
  const combined = card(page, 'Combined')
  await expect(combined).toContainText('600 × 375 px')
  await expect(combined).toContainText('Resize → Grayscale → Compression → Pixelation')
  await expect(combined).toContainText('grid 32×20')

  // Every preview image actually loaded (decoded with a real size).
  await expect.poll(() => page.locator('.result img').evaluateAll(
    (imgs) => imgs.every((i) => i.complete && i.naturalWidth > 0))).toBe(true)

  // No automatic downloads happened; each button downloads the right file.
  for (const r of results.results) {
    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: new RegExp(`^Download ${r.label.replace(/[()]/g, '.')}`) }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(r.filename)
    expect(fs.statSync(await download.path()).size).toBe(r.sizeBytes)
  }

  await page.getByRole('button', { name: 'Process Another Image' }).click()
  await expect(page.locator('.result')).toHaveCount(0)
  await expect(page.locator('.selected-file')).toHaveCount(0)
  await expect(feature(page, 'Resize').locator('.feature-head input')).not.toBeChecked()
  await expect(page.getByRole('button', { name: 'Process Image' })).toBeDisabled()
  await expect(page.locator('input[type=file]')).toBeEnabled()
})

test('PNG: lossless compression (no quality slider), transparency kept', async ({ page }) => {
  await page.goto('/')
  await choose(page, 'logo.png')
  await enable(page, 'Grayscale', 'Compression')
  await expect(feature(page, 'Compression')).toContainText('optimized losslessly')
  await expect(feature(page, 'Compression').locator('input[type=range]')).toHaveCount(0)

  const resultsPromise = resultsResponse(page)
  await page.getByRole('button', { name: 'Process Image' }).click()
  const results = await (await resultsPromise).json()
  expect(results.results.map((r) => r.variant)).toEqual(['grayscale', 'compress', 'combined'])
  await expect(card(page, 'Grayscale')).toContainText('Grayscale (transparency kept)')
  await expect(card(page, 'Compressed')).toContainText('PNG lossless optimization')

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: /^Download Combined/ }).click()
  expect((await downloadPromise).suggestedFilename()).toBe('logo_combined.png')
})

// Invalid input never reaches the backend: fake image, >10 MB, GIF, no feature, bad width.
test('invalid files and options are rejected with clear messages', async ({ page }) => {
  await page.goto('/')
  const error = page.locator('.error')
  const processBtn = page.getByRole('button', { name: 'Process Image' })

  await choose(page, 'notimage.jpg')
  await expect(error).toContainText('not a real JPG or PNG')
  await choose(page, 'big.jpg')
  await expect(error).toContainText('larger than the 10 MB limit')
  await choose(page, 'anim.gif')
  await expect(error).toContainText('Unsupported file type')
  await expect(processBtn).toBeDisabled()

  await choose(page, 'photo.jpg')
  await expect(error).toHaveCount(0)
  await processBtn.click()
  await expect(error).toContainText('Select at least one processing feature.')

  await enable(page, 'Resize')
  await feature(page, 'Resize').getByLabel('Max width (px)').fill('')
  await processBtn.click()
  await expect(error).toContainText('Max width must be a whole number between 1 and 5000.')
  await expect(page.locator('.stages')).toHaveCount(0) // nothing was sent
})

// A truncated JPEG can pass the browser check but must be rejected by Pillow on the
// backend, and the UI must show the FAILED status with the backend's message.
test('a corrupt image that reaches the backend ends as FAILED', async ({ page }) => {
  await page.goto('/')
  await choose(page, 'truncated.jpg')
  // Browsers can show partially-downloaded JPEGs, so the browser may accept this file.
  // Wait for the browser-side check to finish one way or the other.
  await expect(page.locator('.selected-file, .error').first()).toBeVisible()
  const clientRejected = await page.locator('.error').isVisible()
  test.info().annotations.push({ type: 'path', description: clientRejected ? 'rejected in browser' : 'rejected by backend' })
  if (clientRejected) {
    await expect(page.locator('.error')).toContainText('corrupted')
    return
  }
  await enable(page, 'Grayscale')
  await page.getByRole('button', { name: 'Process Image' }).click()
  await expect(page.locator('.badge')).toHaveText('FAILED', { timeout: 60_000 })
  await expect(page.locator('.error')).toContainText('The image file is corrupted or cannot be read.')
  await expect(page.locator('.stages li.failed')).toHaveText('Processing')
})

// page.route() simulates the network dropping while the app polls the job status.
test('network interruption while polling shows an error and recovers', async ({ page }) => {
  await page.goto('/')
  await page.route(JOB_STATUS_URL, (route) => route.abort('internetdisconnected'))
  await choose(page, 'photo.jpg')
  await enable(page, 'Grayscale')
  await page.getByRole('button', { name: 'Process Image' }).click()

  await expect(page.locator('.error')).toContainText('Could not reach the server', { timeout: 30_000 })
  await expect(page.locator('.error')).toContainText('may still be processing')
  await page.unroute(JOB_STATUS_URL)
  await page.getByRole('button', { name: 'Check status again' }).click()
  await expect(page.getByRole('heading', { name: /Results and Preview/ })).toBeVisible({ timeout: 30_000 })
})

// Expired-link handling: near expiry, a click fetches fresh links instead of reprocessing.
// Needs short-lived links (the local server with DOWNLOAD_URL_TTL_SECONDS=30), so it is
// skipped against real AWS, where links last 5 minutes.
test('download links close to expiry are refreshed before downloading', async ({ page }) => {
  await page.goto('/')
  let resultsCalls = 0
  page.on('request', (req) => { if (req.url().endsWith('/results')) resultsCalls += 1 })

  await choose(page, 'photo.jpg')
  await enable(page, 'Grayscale')
  const resultsPromise = resultsResponse(page)
  await page.getByRole('button', { name: 'Process Image' }).click()
  const { expiresInSeconds } = await (await resultsPromise).json()
  test.skip(expiresInSeconds > 60, `dev server uses ${expiresInSeconds}s links; restart it with DOWNLOAD_URL_TTL_SECONDS=30`)
  expect(resultsCalls).toBe(1)

  // Fresh link: downloading does NOT request new URLs.
  let downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: /^Download Grayscale/ }).click()
  await downloadPromise
  expect(resultsCalls).toBe(1)

  // Wait until the link is within the 20-second safety margin of expiring.
  await page.waitForTimeout((expiresInSeconds - 20 + 2) * 1000)
  downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: /^Download Grayscale/ }).click()
  const download = await downloadPromise
  expect(resultsCalls).toBe(2) // a fresh link was fetched; no reprocessing
  expect(fs.statSync(await download.path()).size).toBeGreaterThan(0)
})

// Simulates S3 answering 403 "Request has expired" for a preview image.
test('a preview link rejected by storage (expired) is replaced automatically', async ({ page }) => {
  await page.goto('/')
  let resultsCalls = 0
  page.on('request', (req) => { if (req.url().endsWith('/results')) resultsCalls += 1 })
  let rejected = false
  // Simulate S3 refusing an expired link the first time the preview is requested.
  await page.route(/\/results\/[0-9a-f-]{36}\/pixelate\.jpg/, (route) => {
    if (!rejected && route.request().resourceType() === 'image') {
      rejected = true
      return route.fulfill({ status: 403, contentType: 'application/xml', body: '<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>' })
    }
    return route.continue()
  })

  await choose(page, 'photo.jpg')
  await enable(page, 'Pixelation')
  await page.getByRole('button', { name: 'Process Image' }).click()
  await expect(page.getByRole('heading', { name: /Results and Preview/ })).toBeVisible({ timeout: 60_000 })
  await expect.poll(() => rejected).toBe(true)
  await expect.poll(() => resultsCalls).toBe(2)
  await expect.poll(() => card(page, 'Pixelated').locator('img').evaluate(
    (i) => i.complete && i.naturalWidth > 0)).toBe(true)
  await expect(page.locator('.error')).toHaveCount(0)
})
