"""Image-processing Lambda, triggered by S3 ObjectCreated events on the input bucket.

For each selected operation the user gets one result (that operation applied alone
to the original). If more than one operation is selected, an extra "combined"
result applies all of them in the fixed order: resize, grayscale, compress, pixelate.

Delivery is at-least-once: S3 may send duplicate events and Lambda retries failed
async invocations. Safety comes from conditional DynamoDB claims (see jobs.py) and
deterministic output keys, not from exactly-once execution.

Flow for one upload:
  S3 event -> handler() -> process_record(): claim job in DynamoDB
           -> run_job(): download original, validate, create each result with Pillow,
              upload results to the output bucket, record progress, mark COMPLETED
           -> notify(): SNS email (best effort)
"""

from __future__ import annotations

import json
import logging
import re
# S3 event notifications URL-encode object keys (e.g. spaces become "+").
from urllib.parse import unquote_plus

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError

from . import jobs
from .config import get_settings, jobs_table, s3_client, sns_client
from .image_ops import FORMAT_INFO, MAX_FILE_BYTES, OPERATION_ORDER, ImageValidationError, load_image, process

# Logs go to the CloudWatch log group /aws/lambda/imgprep-processor.
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Only keys shaped like uploads/<job-id>/original are ours; anything else is ignored.
KEY_RE = re.compile(r"^uploads/([0-9a-f-]{36})/original$")

# Human-friendly names shown on each result card in the UI.
LABELS = {
    "resize": "Resized",
    "grayscale": "Grayscale",
    "compress": "Compressed",
    "pixelate": "Pixelated",
    "combined": "Combined (all selected operations)",
}

# INTERVIEW:
# Classifying errors decides whether retrying makes sense. AWS/network errors are
# "transient" (may work next time) -> retry. A broken image is "permanent" -> fail
# immediately, because retrying would just fail again and waste money.
TRANSIENT_ERRORS = (ClientError, BotoCoreError, Boto3Error, ConnectionError, TimeoutError)


class LostClaimError(Exception):
    """Another worker took over this job (our lease expired). Stop quietly."""


# Write one structured (JSON) log line, e.g. {"event": "job_claimed", "jobId": "..."}.
def log(event: str, **fields) -> None:
    logger.info(json.dumps({"event": event, **fields}))


# Decide which result images to create. Example: resize + pixelate selected ->
# [("resize", {resize}), ("pixelate", {pixelate}), ("combined", {resize, pixelate})].
def plan_variants(operations: dict) -> list[tuple[str, dict]]:
    """One variant per selected operation, plus 'combined' when 2+ are selected."""
    selected = [op for op in OPERATION_ORDER if op in operations]
    variants = [(op, {op: operations[op]}) for op in selected]
    if len(selected) > 1:
        variants.append(("combined", {op: operations[op] for op in selected}))
    return variants


def to_plain_ops(operations: dict) -> dict:
    """DynamoDB returns numbers as Decimal; convert to int/bool for Pillow."""
    def conv(v):
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, bool) or v is None:
            return v
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    return conv(operations)


# INTERVIEW:
# SNS (Simple Notification Service) is publish/subscribe messaging: we publish one
# message to a topic and SNS delivers it to every subscriber (here, an email address
# configured in Terraform). Notifications are "best effort": if SNS is down, the job
# must still count as COMPLETED, so every error is caught and only logged.
def notify(settings, job_id: str, status: str, detail: str) -> None:
    """Best effort: an SNS problem must never change the job's outcome."""
    if not settings.sns_topic_arn:
        return
    try:
        # The message contains the job ID and status only - never the job token.
        sns_client(settings).publish(
            TopicArn=settings.sns_topic_arn,
            Subject=f"Image job {status}",
            Message=json.dumps({"jobId": job_id, "status": status, "detail": detail}),
        )
        log("sns_published", jobId=job_id, status=status)
    except Exception as exc:  # noqa: BLE001 - deliberately swallow
        log("sns_publish_failed", jobId=job_id, status=status, error=type(exc).__name__)


# Download the original image from the private input bucket. The Lambda's IAM role
# allows s3:GetObject on uploads/* only (see infra/iam.tf).
def read_input(s3, settings, key: str) -> bytes:
    obj = s3.get_object(Bucket=settings.input_bucket, Key=key)
    # Check the size S3 reports BEFORE reading, so a huge object is never loaded.
    if obj["ContentLength"] > MAX_FILE_BYTES:
        obj["Body"].close()
        raise ImageValidationError("The file is larger than the 10 MB limit.")
    # Read at most limit+1 bytes, as a second guard in case the size was wrong.
    data = obj["Body"].read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ImageValidationError("The file is larger than the 10 MB limit.")
    return data


# Do the actual work for a job we have successfully claimed.
def run_job(job: dict, claim_id: str, settings, table, s3) -> list[dict]:
    job_id = job["jobId"]
    data = read_input(s3, settings, job["inputKey"])
    # Validate the REAL content (magic bytes, integrity, dimensions) with Pillow.
    loaded = load_image(data)
    # A PNG renamed to .jpg is rejected: the declared type must match the content.
    if loaded.format != job["format"]:
        raise ImageValidationError("The file content does not match the declared file type.")

    operations = to_plain_ops(job["operations"])
    variants = plan_variants(operations)
    info = FORMAT_INFO[loaded.format]
    outputs = []
    for index, (variant, ops) in enumerate(variants, start=1):
        # Create one result image in memory...
        result = process(loaded, ops)
        # ...and save it to a deterministic key in the private output bucket. A retry
        # overwrites the same key with identical content instead of adding copies.
        key = jobs.output_key(job_id, variant, info["ext"])
        s3.put_object(Bucket=settings.output_bucket, Key=key, Body=result.data,
                      ContentType=info["content_type"])
        # Metadata stored in DynamoDB and later returned to the browser (minus "key").
        outputs.append({
            "variant": variant,
            "label": LABELS[variant],
            "key": key,
            "format": result.format,
            "extension": info["ext"],
            "contentType": info["content_type"],
            "width": result.width,
            "height": result.height,
            "sizeBytes": len(result.data),
            "operations": result.details["appliedOrder"],
            "details": result.details,
        })
        # Record progress + extend the lease. False means another worker took over
        # (our lease expired), so we stop immediately without writing anything else.
        if not jobs.update_progress(table, job_id, claim_id, completed=index,
                                    total=len(variants), lease_seconds=settings.lease_seconds):
            raise LostClaimError()

    original = {"width": loaded.width, "height": loaded.height,
                "sizeBytes": loaded.size_bytes, "format": loaded.format}
    # Final conditional write: PROCESSING -> COMPLETED, only if we still own the claim.
    if not jobs.complete_job(table, job_id, claim_id, outputs=outputs, original=original):
        raise LostClaimError()
    return outputs


# Handle one S3 event record (normally there is exactly one per invocation).
def process_record(record: dict, settings, table, s3) -> None:
    # Find the job ID inside the object key: uploads/<jobId>/original.
    key = unquote_plus(record["s3"]["object"]["key"])
    match = KEY_RE.match(key)
    if not match:
        log("ignored_key", key=key[:200])
        return
    job_id = match.group(1)

    job = jobs.get_job(table, job_id)
    if job is None:
        log("job_not_found", jobId=job_id)
        return
    # INTERVIEW:
    # Idempotency: processing the same event twice must give the same end result as
    # processing it once. S3 can deliver an event more than once, so an already
    # finished job is simply skipped. (The conditional claim below covers the harder
    # case where two copies of the event arrive at the same time.)
    if job["status"] in (jobs.COMPLETED, jobs.FAILED):
        log("duplicate_event_skipped", jobId=job_id, status=job["status"])
        return

    # Try to become the one worker allowed to process this job (see jobs.claim_job).
    claim = jobs.claim_job(table, job_id, lease_seconds=settings.lease_seconds,
                           max_attempts=settings.max_attempts)
    if claim is None:
        # Someone else holds a valid lease, or the job used up all its attempts.
        current = jobs.get_job(table, job_id) or {}
        if int(current.get("attempts", 0)) >= settings.max_attempts:
            if jobs.fail_stale_job(table, job_id, "Processing failed after several attempts.",
                                   max_attempts=settings.max_attempts, stale_before=jobs.now_ts()):
                notify(settings, job_id, jobs.FAILED, "Processing failed after several attempts.")
        log("claim_not_acquired", jobId=job_id, status=current.get("status"))
        return
    claim_id, job = claim
    attempt = int(job["attempts"])
    log("job_claimed", jobId=job_id, attempt=attempt)

    # Each kind of failure is handled differently (see TRANSIENT_ERRORS above).
    try:
        outputs = run_job(job, claim_id, settings, table, s3)
    # Our lease expired and another worker owns the job now: stop, write nothing.
    except LostClaimError:
        log("claim_lost", jobId=job_id)
        return
    except ImageValidationError as exc:
        # Bad input: retrying cannot help.
        log("job_failed_validation", jobId=job_id, reason=str(exc))
        if jobs.fail_job(table, job_id, str(exc), claim_id=claim_id):
            notify(settings, job_id, jobs.FAILED, str(exc))
        return
    except TRANSIENT_ERRORS as exc:
        # AWS hiccup: give the claim back and let Lambda retry, unless out of attempts.
        log("job_transient_error", jobId=job_id, attempt=attempt, error=type(exc).__name__)
        if attempt >= settings.max_attempts:
            if jobs.fail_job(table, job_id, "Processing failed after several attempts.", claim_id=claim_id):
                notify(settings, job_id, jobs.FAILED, "Processing failed after several attempts.")
            return
        jobs.release_claim(table, job_id, claim_id)
        # Re-raising makes this invocation fail, which triggers Lambda's automatic retry.
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. Pillow MemoryError; not worth retrying
        logger.exception(json.dumps({"event": "job_failed_unexpected", "jobId": job_id}))
        message = "The image could not be processed."
        if jobs.fail_job(table, job_id, message, claim_id=claim_id):
            notify(settings, job_id, jobs.FAILED, message)
        return

    log("job_completed", jobId=job_id, outputs=len(outputs))
    # The job is already COMPLETED in DynamoDB at this point, so even if SNS fails
    # the user still gets their results.
    notify(settings, job_id, jobs.COMPLETED, f"{len(outputs)} processed image(s) ready.")


# INTERVIEW:
# Event-driven architecture: nobody calls this function directly. S3 invokes it
# *asynchronously* whenever an object is created under uploads/ (configured by
# aws_s3_bucket_notification in infra/s3.tf). For async invocations, Lambda retries a
# failed run automatically (2 retries, set in infra/lambda.tf). `event["Records"]`
# holds the S3 event(s): bucket name, object key, size, etc.
def handler(event, context):
    settings = get_settings()
    table = jobs_table(settings)
    s3 = s3_client(settings)
    errors = []
    for record in event.get("Records", []):
        try:
            process_record(record, settings, table, s3)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    if errors:
        # Raising makes Lambda retry the event; finished records are skipped as duplicates.
        raise errors[0]
    return {"processed": len(event.get("Records", []))}
