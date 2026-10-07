"""Tests for the job-management Lambda (API routes) against mocked AWS.

Each test builds an API Gateway-style event, calls job_api.handler() directly (as
Lambda would) and checks the HTTP status code and JSON body. S3 and DynamoDB are
moto mocks (see the `aws` fixture in conftest.py), so no AWS resources are used.
"""

import json
from unittest import mock

import pytest

from conftest import INPUT_BUCKET, api_event, make_image
from imgproc import job_api, jobs

CREATE = "POST /api/jobs"
COMPLETE = "POST /api/jobs/{jobId}/complete-upload"
STATUS = "GET /api/jobs/{jobId}"
RESULTS = "GET /api/jobs/{jobId}/results"


def call(event):
    resp = job_api.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def create(data: bytes, filename="cat.jpg", content_type="image/jpeg", operations=None):
    status, body = call(api_event(CREATE, body={
        "filename": filename,
        "contentType": content_type,
        "fileSize": len(data),
        "operations": operations or {"resize": {}, "grayscale": {}},
    }))
    assert status == 201, body
    return body


def upload(aws, created, data, content_type="image/jpeg"):
    aws["s3"].put_object(Bucket=INPUT_BUCKET, Key=created["upload"]["fields"]["key"],
                         Body=data, ContentType=content_type)


# The happy path of POST /api/jobs: a token comes back, the presigned form targets
# the right key, and the plain token is NOT stored in DynamoDB (only its hash).
def test_create_job_returns_token_and_presigned_post(aws):
    data = make_image("JPEG")
    body = create(data)
    assert body["status"] == "CREATED"
    assert len(body["jobToken"]) >= 43
    fields = body["upload"]["fields"]
    assert fields["key"] == f"uploads/{body['jobId']}/original"
    assert fields["Content-Type"] == "image/jpeg"
    assert "policy" in fields and "x-amz-signature" in fields
    assert body["upload"]["url"].startswith("https://test-input-bucket.s3.ap-south-1.amazonaws.com")
    # Token is never stored in plain text
    item = jobs.get_job(aws["table"], body["jobId"])
    assert body["jobToken"] not in json.dumps(item, default=str)


@pytest.mark.parametrize("body, code", [
    ("not json", "INVALID_JSON"),
    ({"filename": "a.gif", "contentType": "image/gif", "fileSize": 1, "operations": {"grayscale": {}}},
     "VALIDATION_ERROR"),
    ({"filename": "a.jpg", "contentType": "image/jpeg", "fileSize": 100,
      "operations": {"pixelate": {"level": 12}}}, "VALIDATION_ERROR"),
])
def test_create_job_validation_errors(aws, body, code):
    status, resp = call(api_event(CREATE, body=body))
    assert status == 400 and resp["error"]["code"] == code


def test_body_too_large(aws):
    status, resp = call(api_event(CREATE, body="x" * 20000))
    assert status == 413


def test_unknown_route(aws):
    status, _ = call(api_event("DELETE /api/jobs/{jobId}", job_id="x"))
    assert status == 404


# Security test: 401 without a token, 404 with a wrong one, 400 for a malformed ID.
def test_missing_and_wrong_token(aws):
    data = make_image("JPEG")
    created = create(data)
    job_id = created["jobId"]
    status, resp = call(api_event(STATUS, job_id=job_id))
    assert status == 401 and resp["error"]["code"] == "MISSING_TOKEN"
    status, resp = call(api_event(STATUS, job_id=job_id, token="w" * 43))
    assert status == 404 and resp["error"]["code"] == "JOB_NOT_FOUND"
    status, resp = call(api_event(STATUS, job_id="not-a-uuid", token=created["jobToken"]))
    assert status == 400


def test_unknown_job_looks_like_wrong_token(aws):
    status, resp = call(api_event(STATUS, job_id="3f2b8c1e-9d4a-4b6f-8e2a-1c3d5e7f9a0b", token="t" * 43))
    assert status == 404


# Idempotency: calling complete-upload twice is safe.
def test_complete_upload_happy_path_and_idempotent(aws):
    data = make_image("JPEG")
    created = create(data)
    upload(aws, created, data)
    event = api_event(COMPLETE, job_id=created["jobId"], token=created["jobToken"])
    assert call(event) == (200, {"jobId": created["jobId"], "status": "UPLOADED"})
    assert call(event)[1]["status"] == "UPLOADED"  # calling twice is safe


def test_complete_upload_without_file(aws):
    created = create(make_image("JPEG"))
    status, resp = call(api_event(COMPLETE, job_id=created["jobId"], token=created["jobToken"]))
    assert status == 409 and resp["error"]["code"] == "UPLOAD_NOT_FOUND"


def test_complete_upload_size_mismatch_fails_job(aws):
    data = make_image("JPEG")
    created = create(data)
    upload(aws, created, data + b"extra")
    status, resp = call(api_event(COMPLETE, job_id=created["jobId"], token=created["jobToken"]))
    assert status == 400 and resp["error"]["code"] == "UPLOAD_REJECTED"
    assert jobs.get_job(aws["table"], created["jobId"])["status"] == "FAILED"
    assert aws["s3"].list_objects_v2(Bucket=INPUT_BUCKET).get("KeyCount") == 0


# Security test: internal fields never leak into API responses.
def test_status_view_hides_secrets(aws):
    created = create(make_image("JPEG"))
    status, body = call(api_event(STATUS, job_id=created["jobId"], token=created["jobToken"]))
    assert status == 200 and body["status"] == "CREATED"
    assert "tokenHash" not in body and "inputKey" not in body


def test_results_before_completion_is_conflict(aws):
    created = create(make_image("JPEG"))
    status, resp = call(api_event(RESULTS, job_id=created["jobId"], token=created["jobToken"]))
    assert status == 409 and resp["error"]["code"] == "NOT_COMPLETED"


def test_stuck_processing_job_reported_as_failed(aws):
    created = create(make_image("JPEG"))
    table = aws["table"]
    table.update_item(
        Key={"jobId": created["jobId"]},
        UpdateExpression="SET #s = :p, leaseExpiresAt = :old, attempts = :a",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":p": "PROCESSING", ":old": jobs.now_ts() - 1000, ":a": 1},
    )
    _, body = call(api_event(STATUS, job_id=created["jobId"], token=created["jobToken"]))
    assert body["status"] == "FAILED" and "timed out" in body["errorMessage"]


# Failure handling: an unexpected internal error becomes a generic 500 and the
# internal error text is not exposed to the client.
def test_dynamodb_outage_returns_500_without_leaking(aws):
    with mock.patch("imgproc.jobs.create_job", side_effect=RuntimeError("secret internals")):
        status, resp = call(api_event(CREATE, body={
            "filename": "a.jpg", "contentType": "image/jpeg", "fileSize": 10,
            "operations": {"grayscale": {}}}))
    assert status == 500
    assert "secret" not in json.dumps(resp)
