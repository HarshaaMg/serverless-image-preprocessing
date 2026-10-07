// Small display helpers used by the upload and results components.

// 1536 -> "1.5 KB", 2621440 -> "2.50 MB".
export function formatBytes(bytes) {
  if (bytes == null) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/** "23.4% smaller" / "5.1% larger" / null when the change is negligible. */
// Honest reporting: a processed file can be LARGER than the original (e.g. an already
// well-compressed PNG), and the UI says so instead of promising a reduction.
export function sizeChange(original, processed) {
  if (!original || processed == null) return null
  const pct = ((original - processed) / original) * 100
  if (Math.abs(pct) < 0.5) return { text: 'About the same size', tone: 'neutral' }
  return pct > 0
    ? { text: `${pct.toFixed(1)}% smaller`, tone: 'good' }
    : { text: `${Math.abs(pct).toFixed(1)}% larger`, tone: 'warn' }
}

// Friendly names used when showing the order operations were applied in.
export const OPERATION_LABELS = {
  resize: 'Resize',
  grayscale: 'Grayscale',
  compress: 'Compression',
  pixelate: 'Pixelation',
}
