// Option state for step 2 ("Select Processing Features") and helpers that turn it into
// the `operations` object the API expects. The backend (backend/src/imgproc/validation.py)
// enforces the same rules; checking here just gives the user instant feedback.

// Defaults match the project spec: resize 600x400 keeping aspect ratio, JPEG quality 75,
// pixelation level 16. Nothing is selected until the user ticks a feature.
export const DEFAULT_OPTIONS = {
  resize: { enabled: false, maxWidth: 600, maxHeight: 400, keepAspectRatio: true },
  grayscale: { enabled: false },
  compress: { enabled: false, quality: 75 },
  pixelate: { enabled: false, level: 16 },
}

// Returns an error message to show, or null when the options are valid.
export function validateOptions(options) {
  const anySelected = Object.values(options).some((o) => o.enabled)
  if (!anySelected) return 'Select at least one processing feature.'
  if (options.resize.enabled) {
    for (const [name, value] of [['Max width', options.resize.maxWidth], ['Max height', options.resize.maxHeight]]) {
      if (!Number.isInteger(value) || value < 1 || value > 5000) {
        return `${name} must be a whole number between 1 and 5000.`
      }
    }
  }
  return null
}

/** Build the API "operations" object: only enabled operations, only relevant options. */
// Example result: { resize: {maxWidth: 600, maxHeight: 400, keepAspectRatio: true}, pixelate: {level: 16} }
// For PNG, compression is sent as {} (lossless): a JPEG quality is never sent.
export function toOperations(options, isPng) {
  const ops = {}
  if (options.resize.enabled) {
    const { maxWidth, maxHeight, keepAspectRatio } = options.resize
    ops.resize = { maxWidth, maxHeight, keepAspectRatio }
  }
  if (options.grayscale.enabled) ops.grayscale = {}
  if (options.compress.enabled) ops.compress = isPng ? {} : { quality: options.compress.quality }
  if (options.pixelate.enabled) ops.pixelate = { level: options.pixelate.level }
  return ops
}
