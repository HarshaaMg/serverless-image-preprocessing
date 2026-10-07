// Step 2: one checkbox card per operation, with that operation's controls shown only
// when it is ticked.

// Reusable card: a checkbox header plus optional controls (`children`).
function Feature({ checked, onToggle, title, description, disabled, children }) {
  return (
    <div className={`feature ${checked ? 'is-on' : ''}`}>
      <label className="feature-head">
        <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onToggle(e.target.checked)} />
        <span>
          <strong>{title}</strong>
          <small>{description}</small>
        </span>
      </label>
      {checked && <div className="feature-body">{children}</div>}
    </div>
  )
}

// An empty number box becomes NaN so validation can report "must be a whole number".
function parseIntOrNaN(value) {
  return value === '' ? NaN : Number(value)
}

export default function OptionsStep({ options, setOptions, isPng, disabled, error }) {
  // INTERVIEW:
  // React state must be updated immutably: build a NEW object (spread syntax) instead of
  // changing the old one, so React notices the change and re-renders.
  const update = (op, patch) => setOptions((prev) => ({ ...prev, [op]: { ...prev[op], ...patch } }))

  return (
    <section className="card">
      <h2><span className="step">2</span> Select Processing Features</h2>
      <p className="hint">
        You get one result per selected feature. If you select more than one, you also get a
        combined result, applied in this order: Resize &rarr; Grayscale &rarr; Compression &rarr; Pixelation.
      </p>

      {/* A disabled <fieldset> disables every control inside it while a job runs. */}
      <fieldset disabled={disabled} className="features">
        <Feature
          title="Resize" description="Fit inside a maximum width and height"
          checked={options.resize.enabled} onToggle={(v) => update('resize', { enabled: v })}
        >
          <div className="row">
            <label>Max width (px)
              <input type="number" min="1" max="5000" step="1" value={Number.isNaN(options.resize.maxWidth) ? '' : options.resize.maxWidth}
                onChange={(e) => update('resize', { maxWidth: parseIntOrNaN(e.target.value) })} />
            </label>
            <label>Max height (px)
              <input type="number" min="1" max="5000" step="1" value={Number.isNaN(options.resize.maxHeight) ? '' : options.resize.maxHeight}
                onChange={(e) => update('resize', { maxHeight: parseIntOrNaN(e.target.value) })} />
            </label>
          </div>
          <label className="inline">
            <input type="checkbox" checked={options.resize.keepAspectRatio}
              onChange={(e) => update('resize', { keepAspectRatio: e.target.checked })} />
            Keep original aspect ratio (recommended, never stretches; small images are not enlarged)
          </label>
        </Feature>

        <Feature
          title="Grayscale" description="Convert to black and white (transparency is kept)"
          checked={options.grayscale.enabled} onToggle={(v) => update('grayscale', { enabled: v })}
        />

        <Feature
          title="Compression"
          description={isPng ? 'PNG: lossless optimization (no quality loss)' : 'JPEG: choose a quality level'}
          checked={options.compress.enabled} onToggle={(v) => update('compress', { enabled: v })}
        >
          {/* PNG compression is lossless, so the JPEG quality slider is not shown. */}
          {isPng ? (
            <p className="muted">
              PNG files are optimized losslessly, so no quality setting is needed. Some PNGs are already
              well optimized and may not get smaller.
            </p>
          ) : (
            <label>Quality: <strong>{options.compress.quality}</strong> (10 = smallest file, 100 = best quality)
              <input type="range" min="10" max="100" step="1" value={options.compress.quality}
                onChange={(e) => update('compress', { quality: Number(e.target.value) })} />
            </label>
          )}
        </Feature>

        <Feature
          title="Pixelation" description="Blocky, retro look"
          checked={options.pixelate.enabled} onToggle={(v) => update('pixelate', { enabled: v })}
        >
          {/* Slider limited to 8-128 in steps of 8, the same rule the backend enforces. */}
          <label>Pixelation level: <strong>{options.pixelate.level}</strong> blocks along the longer side
            <input type="range" min="8" max="128" step="8" value={options.pixelate.level}
              onChange={(e) => update('pixelate', { level: Number(e.target.value) })} />
          </label>
          <p className="muted">Lower = bigger blocks (more pixelated). Higher = more detail.</p>
        </Feature>
      </fieldset>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  )
}
