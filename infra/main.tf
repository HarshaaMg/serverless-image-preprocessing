# INTERVIEW:
# A "data source" READS information instead of creating anything. Here it looks up the
# AWS account ID of whoever runs Terraform; lambda.tf uses it to restrict which
# account's bucket may invoke the processor Lambda.
data "aws_caller_identity" "current" {}

# Random suffix so bucket names are globally unique.
# S3 bucket names are shared by ALL AWS customers worldwide, so "imgprep-input" alone
# would probably be taken. The value (e.g. 5ef42a93) is generated once and then kept
# in the Terraform state, so bucket names stay stable across applies.
resource "random_id" "suffix" {
  byte_length = 4
}

# INTERVIEW:
# `locals` are named values computed once and reused everywhere (like constants).
# They avoid repeating the same expression in many files. Referenced as local.<name>.
locals {
  name   = var.project_name
  suffix = random_id.suffix.hex

  # Bucket names, e.g. imgprep-input-5ef42a93 and imgprep-output-5ef42a93.
  input_bucket_name  = "${local.name}-input-${local.suffix}"
  output_bucket_name = "${local.name}-output-${local.suffix}"

  # Lambda names are fixed so log group names (/aws/lambda/<name>) are predictable.
  job_api_function_name   = "${local.name}-job-api"
  processor_function_name = "${local.name}-processor"

  # Browser origins allowed by the API and the S3 bucket CORS rules
  # (the React app runs locally at http://localhost:5173).
  cors_origins = var.cors_allowed_origins

  # Lease must outlast one Lambda run so a live worker is never displaced.
  # (Lambda timeout 60 s + 30 s safety margin = 90 s; used by jobs.claim_job.)
  processing_lease_seconds = var.processor_timeout_seconds + 30
}
