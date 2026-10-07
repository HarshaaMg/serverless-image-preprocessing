import { formatBytes, OPERATION_LABELS, sizeChange } from '../format.js'

// Steps 4 and 5: result cards with previews and details, then one download button per
// result. Only results for the operations the user selected are returned by the API, so
// only those are shown.

// Turns the processing details saved by the backend into readable lines.
function Details({ result }) {
  const d = result.details || {}
  const lines = []
  if (d.resize) {
    lines.push(`Resize: max ${d.resize.maxWidth}×${d.resize.maxHeight}` +
      (d.resize.keepAspectRatio ? ', aspect ratio kept' : ', exact size'))
  }
  if (d.grayscale) lines.push(d.grayscale.alphaPreserved ? 'Grayscale (transparency kept)' : 'Grayscale')
  if (d.compress) {
    lines.push(d.compress.method === 'jpeg-quality'
      ? `Compression: JPEG quality ${d.compress.quality}`
      : 'Compression: PNG lossless optimization')
  }
  if (d.pixelate) lines.push(`Pixelation level ${d.pixelate.level} (grid ${d.pixelate.gridWidth}×${d.pixelate.gridHeight})`)
  return <ul className="details">{lines.map((l) => <li key={l}>{l}</li>)}</ul>
}

export default function ResultsStep({ original, originalPreview, results, version, onDownload, onPreviewError, refreshing, error }) {
  return (
    <>
      <section className="card">
        <h2><span className="step">4</span> Results and Preview</h2>
        <p className="hint">
          Preview each result below. Download only the ones you want (step 5). Download links are
          short-lived; if one expires, a fresh link is requested automatically.
        </p>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="results">
          {/* The original is shown for comparison, using the local preview (no download). */}
          <article className="result original">
            <div className="thumb"><img src={originalPreview} alt="Original" /></div>
            <h3>Original</h3>
            <dl>
              <dt>Dimensions</dt><dd>{original?.width} &times; {original?.height} px</dd>
              <dt>File size</dt><dd>{formatBytes(original?.sizeBytes)}</dd>
            </dl>
          </article>
          {results.map((r) => {
            // Size change versus the original, e.g. "64.2% smaller".
            const change = sizeChange(original?.sizeBytes, r.sizeBytes)
            return (
              <article className="result" key={r.variant}>
                <div className="thumb">
                  {/* key changes on every refresh so the browser retries even if the URL is identical */}
                  <img key={`${r.variant}-${version}`} src={r.previewUrl} alt={r.label}
                    onError={() => onPreviewError(r.variant)} />
                </div>
                <h3>{r.label}</h3>
                <p className="muted small">
                  Order: {r.operations.map((op) => OPERATION_LABELS[op]).join(' → ')}
                </p>
                <dl>
                  <dt>Output size</dt><dd><strong>{r.width} &times; {r.height} px</strong></dd>
                  <dt>File size</dt><dd>{formatBytes(r.sizeBytes)}</dd>
                  {change && <><dt>Change</dt><dd className={`tone-${change.tone}`}>{change.text}</dd></>}
                </dl>
                <Details result={r} />
              </article>
            )
          })}
        </div>
      </section>

      <section className="card">
        <h2><span className="step">5</span> Download Processed Images</h2>
        {/* Downloads happen only on click; App.jsx refreshes the link first if it is
            about to expire. */}
        <div className="downloads">
          {results.map((r) => (
            <button type="button" key={r.variant} onClick={() => onDownload(r.variant)} disabled={refreshing}>
              Download {r.label} ({r.extension.toUpperCase()}, {formatBytes(r.sizeBytes)})
            </button>
          ))}
        </div>
        {refreshing && <p className="muted">Getting a fresh download link&hellip;</p>}
      </section>
    </>
  )
}
