// Main React component: owns all the state and runs the whole workflow
//   1 Upload Image -> 2 Select Features -> 3 Processing Status
//   -> 4 Results and Preview -> 5 Download -> 6 Process Another Image
// The four step components in ./components only display things; they receive data and
// callback functions from here as "props".

// INTERVIEW:
// React hooks used here:
//   useState    - a value that, when changed with its setter, re-renders the UI.
//   useRef      - a mutable value that survives re-renders WITHOUT causing one
//                 (good for things like the job token or an AbortController).
//   useEffect   - run code after rendering; here only a cleanup when the app unmounts.
//   useCallback - keep the same function object between renders.
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, completeUpload, createJob, getJob, getResults, uploadToS3 } from './api.js'
import { validateImageFile } from './imageValidation.js'
import { DEFAULT_OPTIONS, toOperations, validateOptions } from './options.js'
import UploadStep from './components/UploadStep.jsx'
import OptionsStep from './components/OptionsStep.jsx'
import StatusStep from './components/StatusStep.jsx'
import ResultsStep from './components/ResultsStep.jsx'
import './App.css'

// How often to ask the backend for the job status, and when to give up waiting.
const POLL_INTERVAL_MS = 1500
const POLL_TIMEOUT_MS = 3 * 60 * 1000
const MAX_CONSECUTIVE_POLL_FAILURES = 5
const URL_SAFETY_MARGIN_MS = 20 * 1000 // refresh links this long before they expire

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Errors worth retrying while polling: network drops and temporary server errors.
const isTemporary = (err) => err instanceof ApiError && (err.code === 'NETWORK' || err.status >= 500)

export default function App() {
  // --- Steps 1 and 2: the chosen file and the selected processing options.
  const [selected, setSelected] = useState(null) // { file, contentType, width, height, previewUrl }
  const [fileError, setFileError] = useState('')
  const [checking, setChecking] = useState(false)
  const [options, setOptions] = useState(DEFAULT_OPTIONS)
  const [optionsError, setOptionsError] = useState('')

  // --- Step 3: where we are in the workflow. `phase` decides which sections render.
  const [phase, setPhase] = useState('setup') // setup | running | done
  const [stage, setStage] = useState(null)
  const [jobStatus, setJobStatus] = useState(null)
  const [progress, setProgress] = useState(null)
  const [runError, setRunError] = useState('')
  const [canRetryCheck, setCanRetryCheck] = useState(false)

  // --- Steps 4 and 5: result metadata plus presigned preview/download links.
  const [results, setResults] = useState(null) // { original, results, expiresAt }
  const [refreshing, setRefreshing] = useState(false)
  const [resultsError, setResultsError] = useState('')

  // INTERVIEW:
  // The job token is a secret for this job only. Keeping it in a ref (memory) instead of
  // localStorage or the URL means it disappears when the tab closes and cannot leak via
  // browser history or other scripts reading storage. Trade-off: a page reload loses
  // the job.
  // The job token lives only in memory: never in the URL, localStorage or logs.
  const jobRef = useRef(null)
  const runIdRef = useRef(0) // bumped on reset so stale async work stops
  const abortRef = useRef(null)
  const lastPreviewRefreshRef = useRef(0)
  const previewUrlRef = useRef(null)
  const resultsVersionRef = useRef(0) // bumped on every link refresh to force <img> reloads

  // PNG files get lossless compression and no quality slider.
  const isPng = selected?.contentType === 'image/png'

  // On unmount: cancel any in-flight requests and free the local preview image memory.
  useEffect(() => () => {
    abortRef.current?.abort()
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
  }, [])

  // Step 1: the user picked a file. Check it in the browser before anything is uploaded.
  async function handleSelect(file) {
    setFileError('')
    setChecking(true)
    try {
      const info = await validateImageFile(file)
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
      previewUrlRef.current = info.previewUrl
      setSelected({ file, ...info })
    } catch (err) {
      setFileError(err.message)
    } finally {
      setChecking(false)
    }
  }

  // Ask the backend for result metadata and FRESH presigned links. `expiresAt` is
  // computed from the browser's own clock, so it does not depend on server time.
  const fetchResults = useCallback(async (signal) => {
    const { jobId, token } = jobRef.current
    const data = await getResults(jobId, token, signal)
    resultsVersionRef.current += 1
    const next = {
      version: resultsVersionRef.current,
      original: data.original,
      results: data.results,
      expiresAt: Date.now() + data.expiresInSeconds * 1000,
    }
    setResults(next)
    return next
  }, [])

  // INTERVIEW:
  // Polling: the browser repeatedly asks "is it done yet?" every 1.5 s. It is the
  // simplest way to follow an asynchronous backend job (alternatives are WebSockets or
  // server push). Short network drops are tolerated (up to 5 failures in a row), and
  // after 3 minutes we stop and offer a "Check status again" button.
  const pollUntilDone = useCallback(async (runId, signal) => {
    const { jobId, token } = jobRef.current
    const started = Date.now()
    let failures = 0
    setStage('processing')
    setRunError('')
    setCanRetryCheck(false)

    // Stop as soon as the user clicked "Process Another Image" (runId changed).
    while (runIdRef.current === runId) {
      try {
        const job = await getJob(jobId, token, signal)
        failures = 0
        setJobStatus(job.status)
        setProgress(job.progress)
        if (job.status === 'COMPLETED') {
          await fetchResults(signal)
          if (runIdRef.current !== runId) return
          setStage('done')
          setPhase('done')
          return
        }
        if (job.status === 'FAILED') {
          setRunError(job.errorMessage || 'Processing failed.')
          return
        }
      } catch (err) {
        if (err.name === 'AbortError') return
        if (!isTemporary(err)) {
          setRunError(err.message)
          return
        }
        failures += 1
        if (failures >= MAX_CONSECUTIVE_POLL_FAILURES) {
          setRunError(`${err.message} Your image may still be processing.`)
          setCanRetryCheck(true)
          return
        }
      }
      if (Date.now() - started > POLL_TIMEOUT_MS) {
        setRunError('Processing is taking longer than expected.')
        setCanRetryCheck(true)
        return
      }
      await sleep(POLL_INTERVAL_MS)
    }
  }, [fetchResults])

  // "Process Image" button: the full upload workflow, one awaited step at a time.
  async function handleStart() {
    if (!selected) {
      setFileError('Choose an image first.')
      return
    }
    // Frontend validation gives instant feedback; the backend checks everything again.
    const problem = validateOptions(options)
    setOptionsError(problem || '')
    if (problem) return

    // A new run ID and AbortController for this attempt, so a reset can cancel it.
    const runId = ++runIdRef.current
    const controller = new AbortController()
    abortRef.current = controller
    setPhase('running')
    setRunError('')
    setCanRetryCheck(false)
    setJobStatus(null)
    setProgress(null)

    try {
      // 1) Create the job: the backend returns a job ID, the job token and a
      //    presigned S3 upload form.
      setStage('creating')
      const job = await createJob({
        filename: selected.file.name,
        contentType: selected.contentType,
        fileSize: selected.file.size,
        operations: toOperations(options, isPng),
      }, controller.signal)
      jobRef.current = { jobId: job.jobId, token: job.jobToken }
      setJobStatus(job.status)

      // 2) Upload the image directly to the private S3 bucket (not through our API).
      setStage('uploading')
      await uploadToS3(job.upload, selected.file, controller.signal)

      // 3) Tell the backend the upload is finished.
      setStage('confirming')
      const confirmed = await completeUpload(job.jobId, job.jobToken, controller.signal)
      setJobStatus(confirmed.status)

      // 4) Wait for the processor Lambda to finish.
      if (runIdRef.current === runId) await pollUntilDone(runId, controller.signal)
    } catch (err) {
      if (err.name === 'AbortError' || runIdRef.current !== runId) return
      setRunError(err.message || 'Something went wrong.')
    }
  }

  // "Check status again" after a network problem or a long wait.
  function handleRetryCheck() {
    if (!jobRef.current) return
    const runId = runIdRef.current
    pollUntilDone(runId, abortRef.current?.signal)
  }

  // INTERVIEW:
  // Presigned download links expire after 5 minutes. Instead of making the user process
  // the image again, we simply ask the results API for new links when the old ones are
  // within 20 seconds of expiring.
  async function freshResults() {
    if (Date.now() < results.expiresAt - URL_SAFETY_MARGIN_MS) return results
    setRefreshing(true)
    try {
      return await fetchResults()
    } finally {
      setRefreshing(false)
    }
  }

  // Step 5: a Download button was clicked. Nothing downloads until the user asks.
  async function handleDownload(variant) {
    setResultsError('')
    try {
      const current = await freshResults()
      const item = current.results.find((r) => r.variant === variant)
      // The link response has "Content-Disposition: attachment", so the browser saves it.
      const a = document.createElement('a')
      a.href = item.downloadUrl
      a.rel = 'noopener'
      document.body.appendChild(a)
      a.click()
      a.remove()
    } catch (err) {
      setResultsError(`Could not get a download link: ${err.message}`)
    }
  }

  // An <img> preview failed to load (S3 returned an error).
  async function handlePreviewError() {
    // Usually means the preview link expired. Refresh at most every 10 seconds.
    if (Date.now() - lastPreviewRefreshRef.current < 10000) return
    lastPreviewRefreshRef.current = Date.now()
    try {
      await fetchResults()
    } catch (err) {
      setResultsError(`Could not reload previews: ${err.message}`)
    }
  }

  // Step 6: "Process Another Image" - cancel anything running and reset every state value.
  function handleReset() {
    runIdRef.current += 1
    abortRef.current?.abort()
    abortRef.current = null
    jobRef.current = null
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
    previewUrlRef.current = null
    setSelected(null)
    setFileError('')
    setOptions(DEFAULT_OPTIONS)
    setOptionsError('')
    setPhase('setup')
    setStage(null)
    setJobStatus(null)
    setProgress(null)
    setRunError('')
    setCanRetryCheck(false)
    setResults(null)
    setResultsError('')
    setRefreshing(false)
  }

  // While a job is running, the file picker and options are disabled.
  const locked = phase !== 'setup'

  // INTERVIEW:
  // Conditional rendering: `{condition && <Component />}` shows a section only when the
  // condition is true. That is how the page moves from setup -> status -> results.
  return (
    <div className="app">
      <header>
        <h1>ImagePreprocessing</h1>
        <p>Resize, convert to grayscale, compress, or pixelate your images in the cloud.</p>
      </header>

      <main>
        {/* Steps 1 and 2 are always visible (locked while a job runs). */}
        <UploadStep selected={selected} error={fileError} checking={checking} disabled={locked} onSelect={handleSelect} />
        <OptionsStep options={options} setOptions={setOptions} isPng={isPng} disabled={locked} error={optionsError} />

        {phase === 'setup' && (
          <div className="actions">
            <button type="button" className="primary" onClick={handleStart} disabled={!selected || checking}>
              Process Image
            </button>
          </div>
        )}

        {/* Step 3: progress, job status and errors. */}
        {phase !== 'setup' && (
          <StatusStep
            stage={stage} jobStatus={jobStatus} progress={progress}
            error={runError} canRetryCheck={canRetryCheck} onRetryCheck={handleRetryCheck}
          />
        )}

        {/* Steps 4 and 5: previews and download buttons, only when the job is done. */}
        {phase === 'done' && results && (
          <ResultsStep
            original={results.original}
            originalPreview={selected?.previewUrl}
            results={results.results}
            version={results.version}
            onDownload={handleDownload}
            onPreviewError={handlePreviewError}
            refreshing={refreshing}
            error={resultsError}
          />
        )}

        {/* Step 6. */}
        {phase !== 'setup' && (
          <section className="card">
            <h2><span className="step">6</span> Process Another Image</h2>
            <button type="button" className="secondary" onClick={handleReset}>Process Another Image</button>
          </section>
        )}
      </main>

      <footer>
        Your images are stored privately and deleted automatically after 1 day.
      </footer>
    </div>
  )
}
