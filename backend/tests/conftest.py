"""Shared test fixtures. All AWS calls are intercepted by moto: nothing reaches AWS.

pytest automatically loads this file before running any test in this folder.
A "fixture" is a function that prepares something a test needs (here: fake AWS
resources and environment variables) and is requested simply by naming it as a
test parameter, e.g. `def test_x(aws): ...`.
"""

import io
import json
import os

import boto3
import pytest
from moto import mock_aws
from PIL import Image

REGION = "ap-south-1"
INPUT_BUCKET = "test-input-bucket"
OUTPUT_BUCKET = "test-output-bucket"
TABLE = "test-jobs"


# autouse=True: applied to EVERY test automatically. monkeypatch sets environment
# variables only for the duration of one test and restores them afterwards.
@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    # Fake credentials so nothing can accidentally talk to a real account.
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "INPUT_BUCKET": INPUT_BUCKET,
        "OUTPUT_BUCKET": OUTPUT_BUCKET,
        "JOBS_TABLE": TABLE,
        "SNS_TOPIC_ARN": "",
        "PROCESSING_LEASE_SECONDS": "90",
        "MAX_PROCESSING_ATTEMPTS": "3",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("LOCAL_AWS_ENDPOINT", "LOCAL_S3_PUBLIC_ENDPOINT", "AWS_PROFILE"):
        monkeypatch.delenv(key, raising=False)


# INTERVIEW:
# Mocking replaces a real dependency (AWS) with a fake one so tests are fast, free and
# repeatable. moto's mock_aws() intercepts every boto3 call in-process. Tests using
# this fixture are integration-style tests of OUR code against a fake AWS - they do
# NOT prove that real IAM permissions, S3 triggers or CORS work (scripts/e2e_test.py
# checks those against the deployed system).
@pytest.fixture
def aws(aws_env, monkeypatch):
    """Mocked S3 buckets, DynamoDB table and SNS topic."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (INPUT_BUCKET, OUTPUT_BUCKET):
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sns = boto3.client("sns", region_name=REGION)
        topic_arn = sns.create_topic(Name="test-topic")["TopicArn"]
        monkeypatch.setenv("SNS_TOPIC_ARN", topic_arn)
        table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        yield {"s3": s3, "table": table, "sns": sns, "topic_arn": topic_arn}


# Helper: build a test image in memory (striped, so compression/pixelation have
# detail to work on) and return its encoded bytes.
def make_image(fmt="JPEG", size=(1200, 800), mode="RGB", color=(200, 30, 60, 255), pattern=True) -> bytes:
    img = Image.new(mode, size, color[: len(mode)] if mode != "L" else 128)
    if pattern:
        # Stripes give pixelation/compression something to work on.
        px = img.load()
        for x in range(0, size[0], 7):
            for y in range(size[1]):
                px[x, y] = (10, 200, 90, 255)[: len(mode)] if mode != "L" else 30
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def open_bytes(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


# Helper: build the same event dict API Gateway (payload v2) sends to the Lambda.
def api_event(route_key, *, job_id=None, token=None, body=None):
    headers = {"content-type": "application/json"}
    if token:
        headers["x-job-token"] = token
    return {
        "routeKey": route_key,
        "headers": headers,
        "pathParameters": {"jobId": job_id} if job_id else None,
        "body": json.dumps(body) if isinstance(body, (dict, list)) else body,
        "isBase64Encoded": False,
    }


# Helper: build the same event dict S3 sends to the processor Lambda.
def s3_event(key, bucket=INPUT_BUCKET):
    return {"Records": [{"eventSource": "aws:s3", "s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
