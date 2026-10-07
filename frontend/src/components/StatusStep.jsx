// Step 3: the progress "pills" (creating -> uploading -> confirming -> processing -> done),
// the backend job status badge, and any error with an optional retry button.

// Browser-side stages of the workflow (the backend has its own job status values).
const STAGES = [
  { key: 'creating', label: 'Creating job' },
  { key: 'uploading', label: 'Uploading image' },
  { key: 'confirming', label: 'Confirming upload' },
  { key: 'processing', label: 'Processing' },
  { key: 'done', label: 'Done' },
]

export default function StatusStep({ stage, jobStatus, progress, error, onRetryCheck, canRetryCheck }) {
  const current = STAGES.findIndex((s) => s.key === stage)
  return (
    <section className="card">
      <h2><span className="step">3</span> Processing Status</h2>
      <ol className="stages">
        {STAGES.map((s, i) => {
          const state = error && i === current ? 'failed' : i < current || stage === 'done' ? 'done' : i === current ? 'active' : ''
          return <li key={s.key} className={state}>{s.label}</li>
        })}
      </ol>
      {jobStatus && <p>Job status: <span className={`badge badge-${jobStatus.toLowerCase()}`}>{jobStatus}</span></p>}
      {/* While processing, show progress reported by the processor Lambda via DynamoDB.
          aria-live lets screen readers announce the updates. */}
      {stage === 'processing' && !error && (
        <div className="progress" aria-live="polite">
          <div className="spinner" aria-hidden="true" />
          <span>
            {progress?.total
              ? `Created ${progress.completed} of ${progress.total} result images…`
              : 'Waiting for the processor…'}
          </span>
        </div>
      )}
      {error && (
        <div className="error" role="alert">
          <p>{error}</p>
          {canRetryCheck && <button type="button" className="secondary" onClick={onRetryCheck}>Check status again</button>}
        </div>
      )}
    </section>
  )
}
