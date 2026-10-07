"""Tests for the S3-triggered processing Lambda and job state safety.

These tests create a job through the real API handler, put the image into the mocked
S3 bucket, then call processor.handler() with an S3-style event - the same sequence
that happens in AWS, but with moto instead of real services. unittest.mock.patch is
used to inject failures (S3 errors, SNS outages, lost claims) on purpose.

INTERVIEW:
Unit test = one function in isolation (test_image_ops.py).
Integration test = several parts working together, here against mocked AWS (this file).
End-to-end test = the whole deployed system through its public interface
(scripts/e2e_test.py against real AWS, frontend/e2e/app.spec.js in a real browser).
"""

import json
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from conftest import INPUT_BUCKET, OUTPUT_BUCKET, api_event, make_image, open_bytes, s3_event
from imgproc import job_api, jobs, processor
from imgproc.config import get_settings


def new_job(aws, data=None, *, filename="pic.jpg", content_type="image/jpeg", operations=None, upload=True):
    data = make_image("JPEG") if data is None else data
    resp = job_api.handler(api_event("POST /api/jobs", body={
        "filename": filename, "contentType": content_type, "fileSize": len(data),
        "operations": operations or {"resize": {}, "grayscale": {}, "pixelate": {"level": 16}},
    }), None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 201, body
    key = body["upload"]["fields"]["key"]
    if upload:
        aws["s3"].put_object(Bucket=INPUT_BUCKET, Key=key, Body=data, ContentType=content_type)
    return body["jobId"], body["jobToken"], key


def run(key):
    return processor.handler(s3_event(key), None)


def job(aws, job_id):
    return jobs.get_job(aws["table"], job_id)


def output_keys(aws):
    return sorted(o["Key"] for o in aws["s3"].list_objects_v2(Bucket=OUTPUT_BUCKET).get("Contents", []))


# ------------------------------------------------------------------ happy path

def test_processing_creates_one_output_per_operation_plus_combined(aws):
    job_id, token, key = new_job(aws)
    run(key)
    item = job(aws, job_id)
    assert item["status"] == "COMPLETED"
    variants = [o["variant"] for o in item["outputs"]]
    assert variants == ["resize", "grayscale", "pixelate", "combined"]
    assert output_keys(aws) == sorted(f"results/{job_id}/{v}.jpg" for v in variants)
    combined = item["outputs"][-1]
    assert combined["operations"] == ["resize", "grayscale", "pixelate"]
    assert (combined["width"], combined["height"]) == (600, 400)
    assert item["original"]["width"] == 1200
    assert "claimId" not in item and "leaseExpiresAt" not in item


def test_single_operation_has_no_combined_output(aws):
    job_id, _, key = new_job(aws, operations={"pixelate": {"level": 32}})
    run(key)
    outputs = job(aws, job_id)["outputs"]
    assert [o["variant"] for o in outputs] == ["pixelate"]
    assert outputs[0]["details"]["pixelate"]["level"] == 32


def test_results_api_returns_presigned_urls(aws):
    job_id, token, key = new_job(aws, operations={"grayscale": {}})
    run(key)
    resp = job_api.handler(api_event("GET /api/jobs/{jobId}/results", job_id=job_id, token=token), None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    result = body["results"][0]
    assert "key" not in result
    assert "X-Amz-Signature" in result["downloadUrl"]
    assert "response-content-disposition=attachment" in result["downloadUrl"]
    assert result["filename"] == "pic_grayscale.jpg"
    # Asking again gives fresh URLs (used when the old ones expire).
    resp2 = job_api.handler(api_event("GET /api/jobs/{jobId}/results", job_id=job_id, token=token), None)
    assert json.loads(resp2["body"])["results"][0]["downloadUrl"]


def test_png_pipeline(aws):
    data = make_image("PNG", (800, 800), mode="RGBA", color=(0, 0, 255, 128))
    job_id, _, key = new_job(aws, data, filename="logo.png", content_type="image/png",
                             operations={"compress": {}, "grayscale": {}})
    run(key)
    item = job(aws, job_id)
    assert item["status"] == "COMPLETED"
    body = aws["s3"].get_object(Bucket=OUTPUT_BUCKET, Key=f"results/{job_id}/combined.png")["Body"].read()
    assert open_bytes(body).mode == "LA"


def test_s3_event_before_complete_upload_still_processes(aws):
    job_id, token, key = new_job(aws)
    assert job(aws, job_id)["status"] == "CREATED"
    run(key)
    assert job(aws, job_id)["status"] == "COMPLETED"
    # A late complete-upload call simply reports the current status.
    resp = job_api.handler(api_event("POST /api/jobs/{jobId}/complete-upload", job_id=job_id, token=token), None)
    assert json.loads(resp["body"])["status"] == "COMPLETED"


def test_url_encoded_key_and_foreign_keys(aws):
    assert run("some/other/file.txt") == {"processed": 1}  # ignored, no crash
    assert run("uploads/3f2b8c1e-9d4a-4b6f-8e2a-1c3d5e7f9a0b/original") == {"processed": 1}  # unknown job


# ------------------------------------------------------------------ bad input

@pytest.mark.parametrize("data, message", [
    (b"definitely not an image", "Unsupported"),
    (make_image("JPEG")[:500], "corrupted"),
])
def test_invalid_upload_marks_job_failed(aws, data, message):
    job_id, _, key = new_job(aws, data)
    run(key)
    item = job(aws, job_id)
    assert item["status"] == "FAILED" and message in item["errorMessage"]
    assert output_keys(aws) == []


def test_content_type_mismatch_fails(aws):
    png = make_image("PNG")
    job_id, _, key = new_job(aws, png, filename="fake.jpg", content_type="image/jpeg")
    run(key)
    assert "does not match" in job(aws, job_id)["errorMessage"]


def test_oversized_object_rejected_without_reading(aws):
    job_id, _, key = new_job(aws, upload=False)
    aws["s3"].put_object(Bucket=INPUT_BUCKET, Key=key, Body=b"\xff\xd8\xff" + b"0" * (10 * 1024 * 1024))
    run(key)
    assert "10 MB" in job(aws, job_id)["errorMessage"]


# ------------------------------------------------------------------ duplicates & concurrency

# INTERVIEW:
# Idempotency test: S3 may deliver the same event twice. The second delivery must
# not run Pillow again and must leave the completed job exactly as it was.
def test_duplicate_event_does_not_reprocess(aws):
    job_id, _, key = new_job(aws)
    run(key)
    first = job(aws, job_id)
    with mock.patch("imgproc.processor.process") as proc:
        run(key)  # duplicate S3 delivery
        proc.assert_not_called()
    assert job(aws, job_id) == first


# Concurrency: while one worker holds a valid lease, a second worker cannot claim.
def test_concurrent_invocation_skips_while_lease_active(aws):
    job_id, _, key = new_job(aws)
    settings = get_settings()
    claim = jobs.claim_job(aws["table"], job_id, lease_seconds=90, max_attempts=3)
    assert claim is not None
    # Second worker arrives while the first still holds a valid lease.
    assert jobs.claim_job(aws["table"], job_id, lease_seconds=90, max_attempts=3) is None
    with mock.patch("imgproc.processor.process") as proc:
        run(key)
        proc.assert_not_called()
    assert job(aws, job_id)["claimId"] == claim[0]


def test_stale_claim_recovery_and_fencing(aws):
    """Worker A dies; after its lease expires worker B takes over. A cannot overwrite B."""
    job_id, _, key = new_job(aws)
    table = aws["table"]
    claim_a, _ = jobs.claim_job(table, job_id, lease_seconds=90, max_attempts=3)
    table.update_item(Key={"jobId": job_id}, UpdateExpression="SET leaseExpiresAt = :t",
                      ExpressionAttributeValues={":t": jobs.now_ts() - 1})  # lease expired

    run(key)  # worker B (a retry) recovers the job
    item = job(aws, job_id)
    assert item["status"] == "COMPLETED" and int(item["attempts"]) == 2

    # Worker A wakes up and tries to write: every fenced update is refused.
    assert not jobs.update_progress(table, job_id, claim_a, completed=1, total=1, lease_seconds=90)
    assert not jobs.complete_job(table, job_id, claim_a, outputs=[], original={})
    assert not jobs.fail_job(table, job_id, "late failure", claim_id=claim_a)
    assert not jobs.release_claim(table, job_id, claim_a)
    assert job(aws, job_id) == item


def test_lost_claim_mid_processing_stops_quietly(aws):
    job_id, _, key = new_job(aws)
    with mock.patch("imgproc.jobs.update_progress", return_value=False):
        run(key)
    item = job(aws, job_id)
    assert item["status"] == "PROCESSING"  # left for the new owner, not overwritten


def test_completed_job_cannot_be_reclaimed(aws):
    job_id, _, key = new_job(aws)
    run(key)
    assert jobs.claim_job(aws["table"], job_id, lease_seconds=90, max_attempts=3) is None


# ------------------------------------------------------------------ partial / transient failures

def _s3_error(*args, **kwargs):
    raise ClientError({"Error": {"Code": "SlowDown", "Message": "slow down"}}, "PutObject")


# Partial failure: the 2nd S3 upload fails. The claim is released, the error is
# re-raised (so Lambda would retry), and the retry finishes the job.
def test_transient_failure_releases_claim_and_retries(aws):
    job_id, _, key = new_job(aws)
    real_client = processor.s3_client

    def flaky_client(settings, **kw):
        client = real_client(settings, **kw)
        calls = {"n": 0}
        original_put = client.put_object

        def put(**params):
            calls["n"] += 1
            if calls["n"] == 2:  # first output saved, second fails (partial failure)
                _s3_error()
            return original_put(**params)
        client.put_object = put
        return client

    with mock.patch("imgproc.processor.s3_client", side_effect=flaky_client):
        with pytest.raises(ClientError):
            run(key)  # raising makes Lambda retry the event
    item = job(aws, job_id)
    assert item["status"] == "UPLOADED" and "claimId" not in item

    run(key)  # the retry succeeds and overwrites the same deterministic keys
    item = job(aws, job_id)
    assert item["status"] == "COMPLETED" and int(item["attempts"]) == 2
    assert len(output_keys(aws)) == 4


def test_gives_up_after_max_attempts(aws):
    job_id, _, key = new_job(aws)
    with mock.patch("imgproc.processor.read_input", side_effect=_s3_error):
        for _ in range(2):
            with pytest.raises(ClientError):
                run(key)
        run(key)  # third attempt: out of retries, job marked FAILED, no exception
    item = job(aws, job_id)
    assert item["status"] == "FAILED" and int(item["attempts"]) == 3


def test_unexpected_processing_error_fails_job(aws):
    job_id, _, key = new_job(aws)
    with mock.patch("imgproc.processor.process", side_effect=MemoryError()):
        run(key)
    assert job(aws, job_id)["status"] == "FAILED"


def test_dynamodb_failure_during_claim_propagates_for_retry(aws):
    job_id, _, key = new_job(aws)
    err = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "UpdateItem")
    with mock.patch("imgproc.jobs.claim_job", side_effect=err):
        with pytest.raises(ClientError):
            run(key)
    assert job(aws, job_id)["status"] == "CREATED"  # untouched; Lambda retry will handle it


# ------------------------------------------------------------------ SNS

# SNS is best effort: a notification outage must not turn a success into a failure.
def test_sns_failure_does_not_fail_completed_job(aws):
    job_id, _, key = new_job(aws)
    with mock.patch("imgproc.processor.sns_client") as sns:
        sns.return_value.publish.side_effect = RuntimeError("SNS is down")
        run(key)
        sns.return_value.publish.assert_called_once()
    assert job(aws, job_id)["status"] == "COMPLETED"


def test_sns_notified_on_completion_and_failure(aws):
    job_id, _, key = new_job(aws)
    bad_id, _, bad_key = new_job(aws, b"not an image at all")
    with mock.patch("imgproc.processor.sns_client") as sns:
        run(key)
        run(bad_key)
    messages = [json.loads(c.kwargs["Message"]) for c in sns.return_value.publish.call_args_list]
    assert [(m["jobId"], m["status"]) for m in messages] == [(job_id, "COMPLETED"), (bad_id, "FAILED")]
    assert all("token" not in json.dumps(m).lower() for m in messages)


def test_no_topic_configured_skips_sns(aws, monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "")
    job_id, _, key = new_job(aws)
    with mock.patch("imgproc.processor.sns_client") as sns:
        run(key)
        sns.assert_not_called()
    assert job(aws, job_id)["status"] == "COMPLETED"
