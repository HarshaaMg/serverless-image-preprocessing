// Unit tests for the frontend's pure logic (no browser, no backend).
// Run with "npm test" (Vitest). Browser APIs that Node does not have (Image, URL blob
// helpers) are replaced with small fakes via vi.stubGlobal, so validateImageFile can be
// tested without a real browser.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { formatBytes, sizeChange } from './format.js'
import { DEFAULT_OPTIONS, toOperations, validateOptions } from './options.js'
import { MAX_FILE_BYTES, validateImageFile } from './imageValidation.js'

const JPEG_MAGIC = [0xff, 0xd8, 0xff, 0xe0, 0, 0, 0, 0]
const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

const fileOf = (name, bytes, size) => {
  const head = new Uint8Array(bytes)
  const body = size && size > head.length ? new Uint8Array(size - head.length) : new Uint8Array(0)
  return new File([head, body], name)
}

const withOptions = (patch) => {
  const opts = structuredClone(DEFAULT_OPTIONS)
  for (const [k, v] of Object.entries(patch)) opts[k] = { ...opts[k], ...v }
  return opts
}

describe('format helpers', () => {
  it('formats byte sizes', () => {
    expect(formatBytes(500)).toBe('500 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.00 MB')
  })
  it('describes size change honestly', () => {
    expect(sizeChange(1000, 750)).toEqual({ text: '25.0% smaller', tone: 'good' })
    expect(sizeChange(1000, 1200)).toEqual({ text: '20.0% larger', tone: 'warn' })
    expect(sizeChange(1000, 999).tone).toBe('neutral')
    expect(sizeChange(undefined, 10)).toBeNull()
  })
})

describe('options', () => {
  it('requires at least one feature', () => {
    expect(validateOptions(DEFAULT_OPTIONS)).toMatch(/at least one/)
  })
  it('validates resize bounds', () => {
    expect(validateOptions(withOptions({ resize: { enabled: true, maxWidth: 0 } }))).toMatch(/Max width/)
    expect(validateOptions(withOptions({ resize: { enabled: true, maxHeight: NaN } }))).toMatch(/Max height/)
    expect(validateOptions(withOptions({ resize: { enabled: true, maxWidth: 12.5 } }))).toMatch(/whole number/)
    expect(validateOptions(withOptions({ resize: { enabled: true } }))).toBeNull()
  })
  it('sends only enabled operations, with defaults 600x400 / 75 / 16', () => {
    const all = withOptions({
      resize: { enabled: true }, grayscale: { enabled: true },
      compress: { enabled: true }, pixelate: { enabled: true },
    })
    expect(toOperations(all, false)).toEqual({
      resize: { maxWidth: 600, maxHeight: 400, keepAspectRatio: true },
      grayscale: {},
      compress: { quality: 75 },
      pixelate: { level: 16 },
    })
    expect(toOperations(withOptions({ grayscale: { enabled: true } }), false)).toEqual({ grayscale: {} })
  })
  it('never sends a JPEG quality for PNG', () => {
    const opts = withOptions({ compress: { enabled: true, quality: 30 } })
    expect(toOperations(opts, true)).toEqual({ compress: {} })
  })
})

describe('validateImageFile', () => {
  let nextImage
  beforeEach(() => {
    nextImage = { width: 800, height: 600, fail: false }
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:test', revokeObjectURL: vi.fn() })
    vi.stubGlobal('Image', class {
      set src(_) {
        setTimeout(() => {
          if (nextImage.fail) this.onerror()
          else {
            this.naturalWidth = nextImage.width
            this.naturalHeight = nextImage.height
            this.onload()
          }
        })
      }
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('accepts a real JPEG and a real PNG', async () => {
    await expect(validateImageFile(fileOf('a.JPG', JPEG_MAGIC))).resolves.toMatchObject({
      contentType: 'image/jpeg', width: 800, height: 600,
    })
    await expect(validateImageFile(fileOf('b.png', PNG_MAGIC))).resolves.toMatchObject({ contentType: 'image/png' })
    await expect(validateImageFile(fileOf('c.jpeg', JPEG_MAGIC))).resolves.toMatchObject({ contentType: 'image/jpeg' })
  })
  it('rejects unsupported extensions', async () => {
    await expect(validateImageFile(fileOf('a.gif', [0x47, 0x49, 0x46]))).rejects.toThrow(/Supported formats/)
    await expect(validateImageFile(fileOf('noext', JPEG_MAGIC))).rejects.toThrow(/Supported formats/)
  })
  it('rejects empty and oversized files', async () => {
    await expect(validateImageFile(fileOf('a.jpg', []))).rejects.toThrow(/empty/)
    await expect(validateImageFile(fileOf('a.jpg', JPEG_MAGIC, MAX_FILE_BYTES + 1))).rejects.toThrow(/10 MB/)
  })
  it('accepts a file of exactly 10 MB', async () => {
    await expect(validateImageFile(fileOf('a.jpg', JPEG_MAGIC, MAX_FILE_BYTES))).resolves.toBeTruthy()
  })
  it('checks real content, not the file name', async () => {
    await expect(validateImageFile(fileOf('a.jpg', [0x68, 0x65, 0x6c, 0x6c, 0x6f]))).rejects.toThrow(/not a real JPG or PNG/)
    await expect(validateImageFile(fileOf('a.jpg', PNG_MAGIC))).rejects.toThrow(/content is PNG/)
  })
  it('rejects undecodable and too-large images', async () => {
    nextImage.fail = true
    await expect(validateImageFile(fileOf('a.jpg', JPEG_MAGIC))).rejects.toThrow(/corrupted/)
    nextImage = { width: 12000, height: 100 }
    await expect(validateImageFile(fileOf('a.jpg', JPEG_MAGIC))).rejects.toThrow(/Maximum width and height/)
    nextImage = { width: 8000, height: 8000 }
    await expect(validateImageFile(fileOf('a.jpg', JPEG_MAGIC))).rejects.toThrow(/too many pixels/)
  })
})
