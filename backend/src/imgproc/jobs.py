"""DynamoDB job records and the job state machine.

Status flow:  CREATED -> UPLOADED -> PROCESSING -> COMPLETED
                                         |  ^
                                         |  | (transient error: claim released for retry)
                                         v  |
                                       FAILED

Every state change is a *conditional* update, so duplicate S3 events, retries and
concurrent Lambda invocations cannot move a job backwards or overwrite newer results.
A worker that claims a job gets a unique claimId plus a lease; only the holder of
the current claimId may record progress, results or failure. If a worker dies, its
lease expires and a retry can take over (stale-claim recovery).

Who calls what:
  job_api.py   -> create_job, get_job, mark_uploaded, fail_job, fail_stale_job
  processor.py -> get_job, claim_job, update_progress, complete_job, fail_job,
                  fail_stale_job, release_claim
"""

from __future__ import annotations

import time
import uuid

# ClientError is how boto3 reports any error returned by an AWS service.
from botocore.exceptions import ClientError

# The five job statuses (stored in the "status" attribute of each DynamoDB item).
CREATED = "CREATED"
UPLOADED = "UPLOADED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"


# Timestamps are stored as whole seconds since 1970 (Unix time), which is also the
# format DynamoDB's TTL feature expects.
def now_ts() -> int:
    return int(time.time())


# INTERVIEW:
# Deterministic S3 keys: the key is computed only from the job ID (and variant), so a
# retry or duplicate event writes to the SAME key instead of creating extra copies.
# That makes repeated processing harmless - a key part of idempotency.
# The upload always lands at uploads/<jobId>/original in the input bucket...
def input_key(job_id: str) -> str:
    return f"uploads/{job_id}/original"


# ...and each result at results/<jobId>/<variant>.<ext> in the output bucket.
def output_key(job_id: str, variant: str, ext: str) -> str:
    return f"results/{job_id}/{variant}.{ext}"


# True when DynamoDB rejected a write because its ConditionExpression was false.
# That is an expected outcome here ("someone else got there first"), not a crash.
def is_conditional_failure(exc: Exception) -> bool:
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    )


# Create the job record when the browser calls POST /api/jobs.
def create_job(table, *, job_id, token_hash, request, ttl_seconds) -> dict:
    now = now_ts()
    item = {
        "jobId": job_id,  # partition key of the table
        "tokenHash": token_hash,  # only the hash; the real token is never stored
        "status": CREATED,
        "originalFilename": request["filename"],
        "contentType": request["contentType"],
        "format": request["format"],
        "declaredSize": request["fileSize"],
        "operations": request["operations"],
        "inputKey": input_key(job_id),
        "attempts": 0,
        "createdAt": now,
        "updatedAt": now,
        "expiresAt": now + ttl_seconds,  # DynamoDB TTL attribute
    }
    # attribute_not_exists(jobId) guarantees we never overwrite an existing job,
    # even in the (practically impossible) case of a UUID collision.
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(jobId)")
    return item


# Read one job. ConsistentRead=True returns the very latest write; without it,
# DynamoDB may briefly return slightly older data (eventual consistency).
def get_job(table, job_id: str) -> dict | None:
    return table.get_item(Key={"jobId": job_id}, ConsistentRead=True).get("Item")


# Called by the complete-upload API after it has checked the object in S3.
def mark_uploaded(table, job_id: str, *, size_bytes: int) -> bool:
    """CREATED -> UPLOADED. Returns False if the job already moved on."""
    try:
        # "#s" is a placeholder for "status" (a reserved word in DynamoDB expressions);
        # ":name" placeholders carry the values. The condition only allows CREATED ->
        # UPLOADED, so a job that is already PROCESSING/COMPLETED is never moved back.
        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #s = :uploaded, uploadedSize = :size, updatedAt = :now",
            ConditionExpression="#s = :created",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":uploaded": UPLOADED,
                ":created": CREATED,
                ":size": size_bytes,
                ":now": now_ts(),
            },
        )
        return True
    except ClientError as exc:
        if is_conditional_failure(exc):
            return False
        raise


# INTERVIEW:
# This is the heart of safe concurrent processing. A DynamoDB *conditional write* is
# atomic: if two Lambda invocations try to claim the same job at the same moment,
# exactly one succeeds and the other gets ConditionalCheckFailedException.
# The lease (leaseExpiresAt) means a crashed worker does not block the job forever:
# once the lease has passed, a retry may claim it ("stale-claim recovery").
# The attempts counter stops endless retries of a job that keeps failing.
# Note: this gives "at-least-once" processing, NOT "exactly-once".
def claim_job(table, job_id: str, *, lease_seconds: int, max_attempts: int) -> tuple[str, dict] | None:
    """Try to take ownership of a job for processing.

    Succeeds if the job is CREATED/UPLOADED (S3 events can arrive before the
    complete-upload call), or PROCESSING with an expired lease (crashed worker).
    Returns (claim_id, updated_job) or None if someone else owns / finished it.
    """
    now = now_ts()
    # A fresh random ID identifies THIS worker's claim (used for "fencing" below).
    claim_id = uuid.uuid4().hex
    try:
        resp = table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET #s = :processing, claimId = :cid, leaseExpiresAt = :lease, "
                "attempts = if_not_exists(attempts, :zero) + :one, updatedAt = :now, "
                "progress = :progress REMOVE errorMessage"
            ),
            ConditionExpression=(
                "attribute_exists(jobId) AND "
                "(#s IN (:created, :uploaded) OR (#s = :processing AND leaseExpiresAt < :now)) AND "
                "(attribute_not_exists(attempts) OR attempts < :max)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":processing": PROCESSING,
                ":created": CREATED,
                ":uploaded": UPLOADED,
                ":cid": claim_id,
                ":lease": now + lease_seconds,
                ":zero": 0,
                ":one": 1,
                ":now": now,
                ":max": max_attempts,
                ":progress": {"completed": 0, "total": 0},
            },
            # Return the updated item so the processor gets the job data in the same call.
            ReturnValues="ALL_NEW",
        )
        return claim_id, resp["Attributes"]
    except ClientError as exc:
        if is_conditional_failure(exc):
            return None
        raise


# INTERVIEW:
# "Fencing": every write made while processing includes `claimId = :cid` in its
# condition. If a slow worker's lease expired and another worker re-claimed the job,
# the old worker's claimId no longer matches, so its late writes are rejected and it
# can never overwrite newer results.
def _claim_condition_values(claim_id: str) -> dict:
    return {":processing": PROCESSING, ":cid": claim_id}


# Called after each result image is saved: records progress (shown in the UI as
# "Created 2 of 5 result images") and pushes the lease forward while work continues.
def update_progress(table, job_id: str, claim_id: str, *, completed: int, total: int, lease_seconds: int) -> bool:
    """Record progress and extend the lease. False if we no longer own the job."""
    now = now_ts()
    try:
        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET progress = :p, leaseExpiresAt = :lease, updatedAt = :now",
            ConditionExpression="#s = :processing AND claimId = :cid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                **_claim_condition_values(claim_id),
                ":p": {"completed": completed, "total": total},
                ":lease": now + lease_seconds,
                ":now": now,
            },
        )
        return True
    except ClientError as exc:
        if is_conditional_failure(exc):
            return False
        raise


# PROCESSING -> COMPLETED, saving the output metadata the results API will return.
# Only the current claim holder can do this (fenced by claimId).
def complete_job(table, job_id: str, claim_id: str, *, outputs: list, original: dict) -> bool:
    now = now_ts()
    try:
        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET #s = :completed, outputs = :outputs, original = :original, "
                "progress = :p, completedAt = :now, updatedAt = :now "
                "REMOVE claimId, leaseExpiresAt, errorMessage"
            ),
            ConditionExpression="#s = :processing AND claimId = :cid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                **_claim_condition_values(claim_id),
                ":completed": COMPLETED,
                ":outputs": outputs,
                ":original": original,
                ":p": {"completed": len(outputs), "total": len(outputs)},
                ":now": now,
            },
        )
        return True
    except ClientError as exc:
        if is_conditional_failure(exc):
            return False
        raise


def fail_job(table, job_id: str, message: str, *, claim_id: str | None = None) -> bool:
    """Mark a job FAILED.

    With claim_id: only if we still own it. Without: only if it was never claimed
    (CREATED/UPLOADED) - used by the API for rejected uploads.
    """
    now = now_ts()
    names = {"#s": "status"}
    # The message is shown to the user, so it is capped in length.
    values = {":failed": FAILED, ":msg": message[:500], ":now": now}
    # Pick the condition that matches who is failing the job (processor vs API).
    if claim_id:
        condition = "#s = :processing AND claimId = :cid"
        values.update(_claim_condition_values(claim_id))
    else:
        condition = "#s IN (:created, :uploaded)"
        values.update({":created": CREATED, ":uploaded": UPLOADED})
    try:
        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET #s = :failed, errorMessage = :msg, updatedAt = :now REMOVE claimId, leaseExpiresAt"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if is_conditional_failure(exc):
            return False
        raise


# Safety net for jobs nobody will finish (e.g. every Lambda retry crashed). The status
# API calls this when it notices a long-expired lease, so the user sees FAILED instead
# of "processing..." forever.
def fail_stale_job(table, job_id: str, message: str, *, max_attempts: int, stale_before: int) -> bool:
    """FAILED for a job that can no longer make progress.

    Either its lease expired long ago, or it has used all attempts. Conditional,
    so a job that a worker just completed or re-claimed is never touched.
    """
    try:
        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET #s = :failed, errorMessage = :msg, updatedAt = :now REMOVE claimId, leaseExpiresAt"
            ),
            ConditionExpression=(
                "(#s = :processing AND leaseExpiresAt < :stale) OR "
                "(#s IN (:created, :uploaded, :processing) AND attempts >= :max "
                " AND (attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt < :now))"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":failed": FAILED,
                ":msg": message[:500],
                ":now": now_ts(),
                ":stale": stale_before,
                ":processing": PROCESSING,
                ":created": CREATED,
                ":uploaded": UPLOADED,
                ":max": max_attempts,
            },
        )
        return True
    except ClientError as exc:
        if is_conditional_failure(exc):
            return False
        raise


# Used when processing hit a temporary AWS error: hand the job back (status UPLOADED,
# no claim) so the automatic Lambda retry can claim it again straight away instead of
# waiting for the lease to expire.
def release_claim(table, job_id: str, claim_id: str) -> bool:
    """PROCESSING -> UPLOADED after a transient error, so a retry can claim it."""
    try:
        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #s = :uploaded, updatedAt = :now REMOVE claimId, leaseExpiresAt",
            ConditionExpression="#s = :processing AND claimId = :cid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                **_claim_condition_values(claim_id),
                ":uploaded": UPLOADED,
                ":now": now_ts(),
            },
        )
        return True
    except ClientError as exc:
        if is_conditional_failure(exc):
            return False
        raise
