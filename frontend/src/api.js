// Small wrapper around the backend API (API Gateway in AWS, dev_server.py locally).

// INTERVIEW:
// Vite exposes environment variables that start with VITE_ as import.meta.env.* at build
// time. Two modes:
//   npm run dev      -> VITE_API_BASE_URL is empty, so calls go to "/api/..." on the Vite
//                       dev server, which forwards them to the local mock backend
//                       (proxy in vite.config.js).
//   npm run dev:aws  -> loads .env.aws.local (written by scripts/configure_frontend.ps1)
//                       with the real API Gateway URL.
const BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '')

// One error type for every failed request, carrying the HTTP status and the backend's
// error code (e.g. VALIDATION_ERROR), so the UI can decide what to show or retry.
export class ApiError extends Error {
  constructor(message, status = 0, code = 'UNKNOWN') {
    super(message)
    this.status = status
    this.code = code
  }
}

// INTERVIEW:
// fetch() is the browser's built-in HTTP client. It only rejects on NETWORK failures;
// an HTTP error such as 400 or 500 still "succeeds", so we must check res.ok ourselves.
// `signal` (from an AbortController) lets the app cancel a request, e.g. on reset.
async function request(path, { method = 'GET', token, body, signal } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  // The job token travels in a header, never in the URL.
  if (token) headers['X-Job-Token'] = token

  let res
  try {
    res = await fetch(BASE_URL + path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
      cache: 'no-store',
    })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    throw new ApiError(
      'Could not reach the server. Check your internet connection and try again.',
      0,
      'NETWORK',
    )
  }

  // Read the JSON body (if any) and turn backend errors into a friendly ApiError.
  let data = null
  try {
    data = await res.json()
  } catch {
    // non-JSON response (e.g. gateway error page)
  }
  if (!res.ok) {
    throw new ApiError(
      data?.error?.message || `Request failed (HTTP ${res.status}).`,
      res.status,
      data?.error?.code,
    )
  }
  return data
}

// One small function per API route (see backend/src/imgproc/job_api.py).
const jobPath = (jobId) => `/api/jobs/${encodeURIComponent(jobId)}`

export const createJob = (payload, signal) =>
  request('/api/jobs', { method: 'POST', body: payload, signal })

export const completeUpload = (jobId, token, signal) =>
  request(`${jobPath(jobId)}/complete-upload`, { method: 'POST', token, signal })

export const getJob = (jobId, token, signal) => request(jobPath(jobId), { token, signal })

export const getResults = (jobId, token, signal) =>
  request(`${jobPath(jobId)}/results`, { token, signal })

/** Upload the file straight to the private S3 bucket using the presigned POST form. */
// INTERVIEW:
// The browser sends the image DIRECTLY to S3 using the signed form fields the backend
// returned - no AWS credentials in the browser and the bucket stays private. S3 checks
// the signature, expiry, exact file size and content type, and answers 204 on success.
// This call goes to the S3 domain, so it relies on the bucket's CORS rules (infra/s3.tf).
export async function uploadToS3(upload, file, signal) {
  const form = new FormData()
  Object.entries(upload.fields).forEach(([key, value]) => form.append(key, value))
  form.append('file', file) // S3 requires the file to be the last field

  let res
  try {
    res = await fetch(upload.url, { method: 'POST', body: form, signal })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    throw new ApiError('The upload was interrupted. Check your connection and try again.', 0, 'NETWORK')
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    if (text.includes('AccessDenied') && text.includes('expired')) {
      throw new ApiError('The upload link expired. Please try again.', res.status, 'UPLOAD_EXPIRED')
    }
    if (text.includes('EntityTooLarge') || text.includes('EntityTooSmall')) {
      throw new ApiError('The file size was rejected by storage (max 10 MB).', res.status, 'UPLOAD_SIZE')
    }
    throw new ApiError(`Upload failed (HTTP ${res.status}).`, res.status, 'UPLOAD_FAILED')
  }
}
