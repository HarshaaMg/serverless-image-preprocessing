import { formatBytes } from '../format.js'

// Step 1: file picker, validation messages and a summary of the selected file.
// It holds no state of its own: App.jsx passes everything in as props.
export default function UploadStep({ selected, error, checking, disabled, onSelect }) {
  return (
    <section className="card">
      <h2><span className="step">1</span> Upload Image</h2>
      <p className="hint">
        Supported formats: <strong>JPG, JPEG, PNG</strong> &middot; Maximum file size: <strong>10 MB</strong>
      </p>
      {/* `accept` only filters the browser's file dialog; real checks happen in
          imageValidation.js and again on the backend. */}
      <label className={`file-picker ${disabled ? 'is-disabled' : ''}`}>
        <input
          type="file"
          accept=".jpg,.jpeg,.png,image/jpeg,image/png"
          disabled={disabled}
          onChange={(e) => {
            const file = e.target.files?.[0]
            e.target.value = '' // allow re-selecting the same file
            if (file) onSelect(file)
          }}
        />
        <span>{selected ? 'Choose a different image' : 'Choose an image'}</span>
      </label>

      {checking && <p className="muted">Checking file&hellip;</p>}
      {error && <p className="error" role="alert">{error}</p>}

      {/* Local preview from a blob: URL - the file has not been uploaded yet. */}
      {selected && (
        <div className="selected-file">
          <img src={selected.previewUrl} alt="Selected" />
          <dl>
            <dt>File</dt><dd className="break">{selected.file.name}</dd>
            <dt>Type</dt><dd>{selected.contentType === 'image/png' ? 'PNG' : 'JPEG'}</dd>
            <dt>Size</dt><dd>{formatBytes(selected.file.size)}</dd>
            <dt>Dimensions</dt><dd>{selected.width} &times; {selected.height} px</dd>
          </dl>
        </div>
      )}
    </section>
  )
}
