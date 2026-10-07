"""Job-management Lambda: handles the four HTTP API routes.

    POST /api/jobs                          create a job, return presigned upload form
    POST /api/jobs/{jobId}/complete-upload  confirm the upload landed in S3
    GET  /api/jobs/{jobId}                  job status, progress and output metadata
    GET  /api/jobs/{jobId}/results          short-lived presigned download URLs

Job-specific routes require the job token in the `X-Job-Token` header.
CORS headers are added by API Gateway (configured in Terraform).

Request path: browser -> API Gateway (infra/api.tf) -> handler() below -> one route
function -> DynamoDB (jobs.py) and/or S3 (presigned URLs). This Lambda never touches
image pixels, so its package does not include Pillow.
"""

from __future__ import annotations

import json
# INTERVIEW:
# Anything written with `logging` (or print) inside Lambda goes to CloudWatch Logs
# automatically, in the log group /aws/lambda/imgprep-job-api. We log small JSON
# objects so logs are easy to search, and we never log tokens.
import logging
import uuid
from decimal import Decimal

from botocore.exceptions import ClientError

from . import jobs, tokens
from .config import get_settings, jobs_table, s3_client
from .limits import MAX_FILE_BYTES
from .validation import ValidationError, is_valid_job_id, parse_create_job

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Request bodies are tiny JSON documents; anything larger is rejected (HTTP 413).
MAX_BODY_BYTES = 16 * 1024
STALE_PROCESSING_GRACE_SECONDS = 300  # after lease expiry, before giving up on a job
STUCK_UPLOADED_SECONDS = 600  # UPLOADED but never picked up by the processor


# One exception type for every "expected" API error. handler() converts it into a
# JSON error response with the right HTTP status code.
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# json.dumps cannot handle Decimal (how DynamoDB returns numbers), so convert it.
def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Not JSON serializable: {type(value)}")


# INTERVIEW:
# With a Lambda "proxy integration", the function must return this exact shape:
# statusCode + headers + body (a JSON *string*). API Gateway turns it into the HTTP
# response. "Cache-Control: no-store" stops browsers caching job data.
def response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=_json_default),
    }


# Every error has the same JSON shape, which frontend/src/api.js knows how to read:
# {"error": {"code": "...", "message": "..."}}
def error_response(status: int, code: str, message: str) -> dict:
    return response(status, {"error": {"code": code, "message": message}})


# Read and parse the JSON request body, rejecting oversized or non-JSON bodies.
def _parse_body(event) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raise ApiError(400, "INVALID_BODY", "Request body must be JSON.")
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "BODY_TOO_LARGE", "Request body is too large.")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise ApiError(400, "INVALID_JSON", "Request body is not valid JSON.")


# INTERVIEW:
# Authorization check for job-specific routes. HTTP status codes used:
#   400 = the request itself is malformed (bad job ID)
#   401 = credentials missing (no X-Job-Token header)
#   404 = job not found OR wrong token - deliberately the SAME answer for both, so an
#         attacker cannot use the API to discover which job IDs exist.
# The token travels in a header, never in the URL, because URLs end up in browser
# history and server access logs.
def _load_authorized_job(event, table) -> dict:
    # API Gateway puts {jobId} from the route path into event["pathParameters"].
    job_id = (event.get("pathParameters") or {}).get("jobId")
    if not is_valid_job_id(job_id):
        raise ApiError(400, "INVALID_JOB_ID", "Invalid job ID.")
    # HTTP header names are case-insensitive, so normalize them to lowercase.
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = headers.get("x-job-token")
    if not token:
        raise ApiError(401, "MISSING_TOKEN", "The X-Job-Token header is required.")
    job = jobs.get_job(table, job_id)
    # Same response for unknown job and wrong token, so job IDs cannot be probed.
    if not job or not tokens.verify_token(token, job.get("tokenHash")):
        raise ApiError(404, "JOB_NOT_FOUND", "Job not found or token is invalid.")
    return job


# Build the browser-facing copy of a job record. Internal fields (token hash, S3 keys,
# claim/lease details) are left out on purpose.
def public_job_view(job: dict) -> dict:
    """Job fields safe to return to the browser (never the token hash or S3 keys)."""
    outputs = [
        {k: v for k, v in out.items() if k != "key"} for out in job.get("outputs", [])
    ]
    return {
        "jobId": job["jobId"],
        "status": job["status"],
        "originalFilename": job.get("originalFilename"),
        "format": job.get("format"),
        "operations": job.get("operations"),
        "progress": job.get("progress"),
        "original": job.get("original"),
        "outputs": outputs,
        "errorMessage": job.get("errorMessage"),
        "createdAt": job.get("createdAt"),
        "updatedAt": job.get("updatedAt"),
        "completedAt": job.get("completedAt"),
    }


# ------------------------------------------------------------------ routes


# POST /api/jobs - step 1 of the workflow.
def create_job(event, settings, table) -> dict:
    # Validate the body first; any problem becomes HTTP 400 with a clear message.
    try:
        request = parse_create_job(_parse_body(event))
    except ValidationError as exc:
        raise ApiError(400, "VALIDATION_ERROR", str(exc))

    # INTERVIEW:
    # A UUID v4 is 122 random bits: unique without asking a central database for the
    # "next number", which suits distributed/serverless systems. It links together the
    # API response, the DynamoDB record and the S3 keys of this job.
    job_id = str(uuid.uuid4())
    # The plain token goes back to the browser once; only its hash is saved.
    token = tokens.new_token()
    jobs.create_job(
        table,
        job_id=job_id,
        token_hash=tokens.hash_token(token),
        request=request,
        ttl_seconds=settings.job_ttl_seconds,
    )

    # INTERVIEW:
    # A presigned URL/form grants temporary permission (here 5 minutes) for ONE action
    # on ONE object, signed with this Lambda role's credentials. The bucket stays
    # private and no AWS credentials ever reach the browser. The image goes straight
    # from the browser to S3, so it never passes through API Gateway or Lambda
    # (which have payload limits and would cost more).
    # Presigned POST lets S3 itself enforce the exact size and content type.
    upload = s3_client(settings, for_presign=True).generate_presigned_post(
        Bucket=settings.input_bucket,
        Key=jobs.input_key(job_id),
        Fields={"Content-Type": request["contentType"]},
        # Policy conditions S3 checks on upload: exact Content-Type, and a size range
        # whose min and max are both the declared size (so "exactly N bytes").
        Conditions=[
            {"Content-Type": request["contentType"]},
            ["content-length-range", request["fileSize"], request["fileSize"]],
        ],
        ExpiresIn=settings.upload_url_ttl,
    )
    logger.info(json.dumps({"event": "job_created", "jobId": job_id, "format": request["format"],
                            "operations": sorted(request["operations"])}))
    # 201 Created: a new resource (the job) was created.
    return response(201, {
        "jobId": job_id,
        "jobToken": token,
        "status": jobs.CREATED,
        "upload": {"url": upload["url"], "fields": upload["fields"],
                   "expiresInSeconds": settings.upload_url_ttl},
        "operations": request["operations"],
    })


# POST /api/jobs/{jobId}/complete-upload - the browser says "I finished uploading".
def complete_upload(event, settings, table) -> dict:
    job = _load_authorized_job(event, table)
    job_id = job["jobId"]

    # INTERVIEW:
    # Idempotent endpoint: calling it twice (e.g. the browser retried) is safe and
    # returns the current status instead of an error. The S3 event may even have
    # triggered processing before this call arrived.
    if job["status"] != jobs.CREATED:
        # Already handled (e.g. retry of this call, or S3 event arrived first).
        return response(200, {"jobId": job_id, "status": job["status"]})

    # Look at the uploaded object's metadata (size, type) without downloading it.
    s3 = s3_client(settings)
    try:
        head = s3.head_object(Bucket=settings.input_bucket, Key=job["inputKey"])
    except ClientError as exc:
        # Without s3:ListBucket (deliberately not granted) S3 reports a missing object as 403.
        if exc.response.get("Error", {}).get("Code") in ("403", "404", "NoSuchKey", "NotFound"):
            logger.info(json.dumps({"event": "upload_head_failed", "jobId": job_id,
                                    "code": exc.response.get("Error", {}).get("Code")}))
            # 409 Conflict: the request is valid but the job is not in the right state yet.
            raise ApiError(409, "UPLOAD_NOT_FOUND", "The uploaded file was not found. Please upload it first.")
        raise

    # Defence in depth: S3 already enforces the size/type from the signed form, but we
    # double-check, reject the job and delete the object if anything does not match.
    size = head["ContentLength"]
    problem = None
    if size > MAX_FILE_BYTES:
        problem = "The uploaded file is larger than the 10 MB limit."
    elif size != int(job["declaredSize"]):
        problem = "The uploaded file size does not match the declared size."
    elif head.get("ContentType") != job["contentType"]:
        problem = "The uploaded file type does not match the declared type."
    if problem:
        jobs.fail_job(table, job_id, problem)
        s3.delete_object(Bucket=settings.input_bucket, Key=job["inputKey"])
        raise ApiError(400, "UPLOAD_REJECTED", problem)

    jobs.mark_uploaded(table, job_id, size_bytes=size)
    current = jobs.get_job(table, job_id)
    return response(200, {"jobId": job_id, "status": current["status"]})


# If a job looks abandoned (its worker's lease expired long ago, or it was uploaded
# but never picked up), mark it FAILED so the user gets an answer instead of waiting
# forever. Runs whenever the browser polls the status.
def _expire_if_stuck(job, settings, table) -> dict:
    now = jobs.now_ts()
    status = job["status"]
    changed = False
    if status == jobs.PROCESSING and int(job.get("leaseExpiresAt", now)) < now - STALE_PROCESSING_GRACE_SECONDS:
        changed = jobs.fail_stale_job(
            table, job["jobId"], "Processing timed out. Please try again.",
            max_attempts=settings.max_attempts, stale_before=now - STALE_PROCESSING_GRACE_SECONDS,
        )
    elif status == jobs.UPLOADED and int(job.get("updatedAt", now)) < now - STUCK_UPLOADED_SECONDS:
        changed = jobs.fail_job(table, job["jobId"], "Processing did not start. Please try again.")
    return jobs.get_job(table, job["jobId"]) if changed else job


# GET /api/jobs/{jobId} - polled by the browser every 1.5 seconds while processing.
def get_status(event, settings, table) -> dict:
    job = _load_authorized_job(event, table)
    job = _expire_if_stuck(job, settings, table)
    return response(200, public_job_view(job))


# GET /api/jobs/{jobId}/results - fresh presigned download/preview links.
# The browser calls this again whenever its links are about to expire, so an
# expired link never means re-processing the image.
def get_results(event, settings, table) -> dict:
    job = _load_authorized_job(event, table)
    if job["status"] != jobs.COMPLETED:
        raise ApiError(409, "NOT_COMPLETED", f"Job is not completed (status: {job['status']}).")

    s3 = s3_client(settings, for_presign=True)
    # Build a safe download file name like "holiday_photo_resize.jpg" from the original
    # name (only letters, digits, - and _), so it cannot break the HTTP header.
    base = job["originalFilename"].rsplit(".", 1)[0][:100]
    safe_base = "".join(c if c.isalnum() or c in "-_" else "_" for c in base) or "image"
    results = []
    for out in job.get("outputs", []):
        filename = f"{safe_base}_{out['variant']}.{out['extension']}"
        # Download link: S3 adds "Content-Disposition: attachment", so the browser saves
        # the file instead of opening it.
        params = {
            "Bucket": settings.output_bucket,
            "Key": out["key"],
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
            "ResponseContentType": out["contentType"],
        }
        # Preview link: same object without "attachment", used by the <img> tags.
        view_params = {k: v for k, v in params.items() if k != "ResponseContentDisposition"}
        result = {k: v for k, v in out.items() if k != "key"}
        result["filename"] = filename
        result["downloadUrl"] = s3.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=settings.download_url_ttl)
        result["previewUrl"] = s3.generate_presigned_url(
            "get_object", Params=view_params, ExpiresIn=settings.download_url_ttl)
        results.append(result)

    return response(200, {
        "jobId": job["jobId"],
        "status": job["status"],
        "original": job.get("original"),
        "results": results,
        "expiresInSeconds": settings.download_url_ttl,
    })


# Route table: the keys match the route keys created in infra/api.tf exactly.
ROUTES = {
    "POST /api/jobs": create_job,
    "POST /api/jobs/{jobId}/complete-upload": complete_upload,
    "GET /api/jobs/{jobId}": get_status,
    "GET /api/jobs/{jobId}/results": get_results,
}


# INTERVIEW:
# The Lambda handler is the function AWS calls for every request
# (configured as handler = "imgproc.job_api.handler" in infra/lambda.tf).
# `event` is the HTTP request converted to a dict by API Gateway (payload format 2.0);
# `context` carries runtime info such as the request ID. One Lambda serves all four
# routes by looking at event["routeKey"], e.g. "GET /api/jobs/{jobId}".
# CORS is NOT handled here: API Gateway answers browser preflight (OPTIONS) requests
# and adds the CORS headers itself.
def handler(event, context):
    route = event.get("routeKey", "")
    func = ROUTES.get(route)
    if func is None:
        return error_response(404, "NOT_FOUND", "Route not found.")
    # Never log the whole event: its headers contain the job token.
    request_id = getattr(context, "aws_request_id", "-")
    try:
        settings = get_settings()
        return func(event, settings, jobs_table(settings))
    # Expected problems (bad input, wrong token...) -> their own 4xx status code.
    except ApiError as exc:
        logger.info(json.dumps({"event": "api_error", "route": route, "status": exc.status,
                                "code": exc.code, "requestId": request_id}))
        return error_response(exc.status, exc.code, exc.message)
    # Anything unexpected -> generic 500. The full stack trace goes to CloudWatch,
    # but the user only sees a safe message (no internal details leak out).
    except Exception:
        logger.exception(json.dumps({"event": "unhandled_error", "route": route, "requestId": request_id}))
        return error_response(500, "INTERNAL_ERROR", "Something went wrong. Please try again.")
