// Client-side checks give quick feedback. The backend re-checks everything,
// because anything done in the browser can be bypassed.

// INTERVIEW:
// Frontend validation improves the user experience (instant, clear errors before any
// upload), but it is NOT security: anyone can bypass the browser and call the API
// directly. The backend repeats every check (validation.py and image_ops.py).

export const MAX_FILE_BYTES = 10 * 1024 * 1024
export const MAX_DIMENSION = 10000
export const MAX_PIXELS = 40_000_000

const EXTENSION_TYPES = { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png' }

/** Real file type from the first bytes ("magic numbers"), not the name or browser MIME type. */
async function sniffType(file) {
  const bytes = new Uint8Array(await file.slice(0, 8).arrayBuffer())
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg'
  const png = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
  if (png.every((b, i) => bytes[i] === b)) return 'image/png'
  return null
}

// Let the browser decode the image to learn its width/height (also proves it decodes).
function readDimensions(url) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight })
    img.onerror = () => reject(new Error('decode failed'))
    img.src = url
  })
}

/**
 * Returns { contentType, width, height, previewUrl } or throws Error with a friendly message.
 * The caller must URL.revokeObjectURL(previewUrl) when done.
 */
export async function validateImageFile(file) {
  const ext = file.name.includes('.') ? file.name.split('.').pop().toLowerCase() : ''
  if (!EXTENSION_TYPES[ext]) {
    throw new Error('Unsupported file type. Supported formats: JPG, JPEG, PNG.')
  }
  if (file.size === 0) throw new Error('The file is empty.')
  if (file.size > MAX_FILE_BYTES) {
    throw new Error('The file is larger than the 10 MB limit.')
  }
  const contentType = await sniffType(file)
  if (!contentType) {
    throw new Error('This file is not a real JPG or PNG image. Supported formats: JPG, JPEG, PNG.')
  }
  if (contentType !== EXTENSION_TYPES[ext]) {
    const actual = contentType === 'image/png' ? 'PNG' : 'JPEG'
    throw new Error(`The file content is ${actual}, but its name ends in .${ext}. Please rename it.`)
  }

  // A blob: URL points at the local file in memory, used for the preview <img>.
  const previewUrl = URL.createObjectURL(file)
  try {
    const { width, height } = await readDimensions(previewUrl)
    if (width > MAX_DIMENSION || height > MAX_DIMENSION) {
      throw new Error(`The image is ${width}x${height}. Maximum width and height is ${MAX_DIMENSION} pixels.`)
    }
    if (width * height > MAX_PIXELS) {
      throw new Error('The image has too many pixels (maximum 40 megapixels).')
    }
    return { contentType, width, height, previewUrl }
  } catch (err) {
    URL.revokeObjectURL(previewUrl)
    if (err.message === 'decode failed') {
      throw new Error('The image file is corrupted or cannot be read.', { cause: err })
    }
    throw err
  }
}
