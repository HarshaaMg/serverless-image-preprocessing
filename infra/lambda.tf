# Lambda packages are built by scripts/build_lambda.ps1 into ../build/ BEFORE plan/apply.
# The processor package contains Linux (manylinux) Pillow wheels, not Windows ones.

# INTERVIEW:
# Lambda packaging: a Lambda function's code is uploaded as a zip. The archive_file data
# source zips each build folder and gives a hash of the zip; Terraform redeploys the
# function only when that hash (source_code_hash below) changes.
# - job_api zip: just our imgproc package (boto3 is already in the Lambda runtime).
# - processor zip: imgproc + Pillow compiled for Linux x86_64 (Lambda runs on Amazon
#   Linux, so a Windows Pillow build would fail to import there).
data "archive_file" "job_api" {
  type        = "zip"
  source_dir  = "${path.module}/../build/job_api"
  output_path = "${path.module}/../build/job_api.zip"
}

data "archive_file" "processor" {
  type        = "zip"
  source_dir  = "${path.module}/../build/processor"
  output_path = "${path.module}/../build/processor.zip"
}

# INTERVIEW:
# CloudWatch Logs: Lambda writes everything the code logs to the log group named
# /aws/lambda/<function-name>. Creating the groups ourselves (instead of letting Lambda
# create them on first run) lets us set a retention period, so logs do not pile up and
# cost money forever.
resource "aws_cloudwatch_log_group" "job_api" {
  name              = "/aws/lambda/${local.job_api_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "processor" {
  name              = "/aws/lambda/${local.processor_function_name}"
  retention_in_days = var.log_retention_days
}

# Environment variables shared by both functions. The Python code reads them in
# backend/src/imgproc/config.py, so no names or IDs are hard-coded in the code.
locals {
  common_env = {
    INPUT_BUCKET             = aws_s3_bucket.images["input"].id
    OUTPUT_BUCKET            = aws_s3_bucket.images["output"].id
    JOBS_TABLE               = aws_dynamodb_table.jobs.name
    PROCESSING_LEASE_SECONDS = tostring(local.processing_lease_seconds)
    MAX_PROCESSING_ATTEMPTS  = "3"
    UPLOAD_URL_TTL_SECONDS   = "300"
    DOWNLOAD_URL_TTL_SECONDS = "300"
    JOB_TTL_SECONDS          = tostring(var.file_retention_days * 86400)
  }
}

# INTERVIEW:
# AWS Lambda runs our code without servers to manage: AWS starts it on demand, scales it
# automatically and bills per request and per millisecond of run time. `handler` tells
# Lambda which Python function to call: module imgproc.job_api, function handler.
# `role` is the IAM role whose permissions the code runs with (iam.tf).
# Job-management Lambda: serves the 4 API routes (called by API Gateway, api.tf).
resource "aws_lambda_function" "job_api" {
  function_name    = local.job_api_function_name
  description      = "ImagePreprocessing job management API"
  role             = aws_iam_role.job_api.arn
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  handler          = "imgproc.job_api.handler"
  filename         = data.archive_file.job_api.output_path
  source_code_hash = data.archive_file.job_api.output_base64sha256
  # Small and fast: it only talks to DynamoDB and signs URLs.
  memory_size = 256
  timeout     = 10

  environment {
    variables = local.common_env
  }

  # Create the log group and the permissions before the function, so its very first
  # run can already write logs.
  depends_on = [aws_cloudwatch_log_group.job_api, aws_iam_role_policy.job_api]
}

# Image-processing Lambda: triggered by S3 uploads (s3.tf), runs Pillow.
# More memory also means more CPU in Lambda, which speeds up image processing.
resource "aws_lambda_function" "processor" {
  function_name                  = local.processor_function_name
  description                    = "ImagePreprocessing image processor (Pillow)"
  role                           = aws_iam_role.processor.arn
  runtime                        = "python3.12"
  architectures                  = ["x86_64"]
  handler                        = "imgproc.processor.handler"
  filename                       = data.archive_file.processor.output_path
  source_code_hash               = data.archive_file.processor.output_base64sha256
  memory_size                    = var.processor_memory_mb
  timeout                        = var.processor_timeout_seconds
  reserved_concurrent_executions = var.processor_reserved_concurrency

  # Same variables as the API Lambda, plus the SNS topic it publishes to.
  environment {
    variables = merge(local.common_env, {
      SNS_TOPIC_ARN = aws_sns_topic.notifications.arn
    })
  }

  depends_on = [aws_cloudwatch_log_group.processor, aws_iam_role_policy.processor]
}

# S3 invokes the processor asynchronously; Lambda retries failures twice.
# Events older than 15 minutes are dropped instead of retried forever.
resource "aws_lambda_function_event_invoke_config" "processor" {
  function_name                = aws_lambda_function.processor.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 900
}

# INTERVIEW:
# A resource-based permission on the Lambda: it allows the S3 service to invoke this
# function, but ONLY for events coming from our input bucket in our own account
# (source_arn + source_account). IAM roles say what a Lambda may do; this says who may
# call the Lambda.
resource "aws_lambda_permission" "s3_invoke_processor" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.processor.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.images["input"].arn
  source_account = data.aws_caller_identity.current.account_id
}
