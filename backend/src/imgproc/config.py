"""Configuration from environment variables (set by Terraform or the local dev server).

Both Lambda functions call get_settings() at the start of every invocation and use the
helper functions below to create AWS SDK (boto3) clients. Nothing here is hard-coded to
one AWS account: bucket names, table name and topic ARN all come from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# boto3 is the official AWS SDK for Python. The Lambda runtime already includes it,
# so it is NOT packaged into the Lambda zip (see backend/requirements.txt).
import boto3
from botocore.config import Config


# One immutable object holding every setting the code needs.
# frozen=True means the values cannot be changed accidentally after creation.
@dataclass(frozen=True)
class Settings:
    region: str
    input_bucket: str
    output_bucket: str
    jobs_table: str
    sns_topic_arn: str
    upload_url_ttl: int
    download_url_ttl: int
    job_ttl_seconds: int
    lease_seconds: int
    max_attempts: int
    s3_public_endpoint: str | None  # for local development only
    aws_endpoint: str | None  # for local development only


# INTERVIEW:
# Environment variables keep configuration out of the code. The same code runs
# locally (pointing at a mock) and in AWS (pointing at real resources) just by
# changing variables. Terraform sets these on each Lambda in infra/lambda.tf.
# AWS credentials are NOT read here: in Lambda, boto3 automatically uses the
# temporary credentials of the function's IAM role.
def get_settings() -> Settings:
    env = os.environ
    return Settings(
        region=env.get("AWS_REGION", "ap-south-1"),
        # env["..."] (no default) makes the function fail fast if a required
        # variable is missing, instead of silently using a wrong value.
        input_bucket=env["INPUT_BUCKET"],
        output_bucket=env["OUTPUT_BUCKET"],
        jobs_table=env["JOBS_TABLE"],
        # Empty string = "no SNS topic configured"; the processor then skips notifications.
        sns_topic_arn=env.get("SNS_TOPIC_ARN", ""),
        # How long presigned upload/download URLs stay valid (seconds).
        upload_url_ttl=int(env.get("UPLOAD_URL_TTL_SECONDS", "300")),
        download_url_ttl=int(env.get("DOWNLOAD_URL_TTL_SECONDS", "300")),
        # How long a job record lives before DynamoDB TTL deletes it (1 day).
        job_ttl_seconds=int(env.get("JOB_TTL_SECONDS", "86400")),
        # Processing lease and retry limit, used by the claim logic in jobs.py.
        lease_seconds=int(env.get("PROCESSING_LEASE_SECONDS", "90")),
        max_attempts=int(env.get("MAX_PROCESSING_ATTEMPTS", "3")),
        # Only set by backend/local/dev_server.py, to point boto3 at the moto mock.
        s3_public_endpoint=env.get("LOCAL_S3_PUBLIC_ENDPOINT") or None,
        aws_endpoint=env.get("LOCAL_AWS_ENDPOINT") or None,
    )


# Shared SDK settings: retry transient AWS errors up to 3 times and never hang
# forever on a network call (important inside a Lambda with a time limit).
_RETRY = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20)


# Create an S3 client. for_presign=True is used when the client will only *sign*
# URLs for the browser (locally the browser needs a different host than the server).
def s3_client(settings: Settings, *, for_presign: bool = False):
    # Local development: talk to the moto mock server instead of real AWS.
    if settings.aws_endpoint:
        endpoint = settings.s3_public_endpoint if for_presign else settings.aws_endpoint
        return boto3.client(
            "s3",
            region_name=settings.region,
            endpoint_url=endpoint,
            config=_RETRY.merge(Config(signature_version="s3v4", s3={"addressing_style": "path"})),
        )
    # Regional endpoint: avoids redirects (which break browser CORS) for new buckets.
    # Presigned URLs therefore look like https://<bucket>.s3.ap-south-1.amazonaws.com/...
    return boto3.client(
        "s3",
        region_name=settings.region,
        endpoint_url=f"https://s3.{settings.region}.amazonaws.com",
        config=_RETRY.merge(Config(signature_version="s3v4", s3={"addressing_style": "virtual"})),
    )


# Return the DynamoDB jobs table. The "resource" API lets us read/write Python
# dicts directly (numbers come back as Decimal, which the code converts).
def jobs_table(settings: Settings):
    resource = boto3.resource(
        "dynamodb", region_name=settings.region, endpoint_url=settings.aws_endpoint, config=_RETRY
    )
    return resource.Table(settings.jobs_table)


# SNS client used by the processor to send completion/failure emails.
def sns_client(settings: Settings):
    return boto3.client(
        "sns", region_name=settings.region, endpoint_url=settings.aws_endpoint, config=_RETRY
    )
