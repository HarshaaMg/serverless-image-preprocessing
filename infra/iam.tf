# Least-privilege roles: each Lambda can touch only the exact resources/prefixes it needs.

# INTERVIEW:
# An IAM role is an identity with permissions but no password or long-term keys. A
# Lambda "assumes" its role and receives short-lived credentials automatically, which
# is why no AWS keys appear anywhere in the code. This "trust policy" says WHO may
# assume the roles below: only the Lambda service.
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------- job API

# Role used by the job-management Lambda.
resource "aws_iam_role" "job_api" {
  name               = "${local.name}-job-api-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# INTERVIEW:
# Least privilege: grant only the actions the code actually calls, on only the exact
# resources it uses (one log group, one table, one key prefix per bucket) - never "*".
# If this Lambda were compromised, it still could not delete the table, read other
# buckets or touch other AWS services. Compare these statements with the boto3 calls
# in backend/src/imgproc/job_api.py.
data "aws_iam_policy_document" "job_api" {
  # Write its own log lines to CloudWatch.
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.job_api.arn}:*"]
  }

  # Create jobs, read them, and update their status. No Delete, no Scan.
  statement {
    sid       = "JobsTable"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.jobs.arn]
  }

  # PutObject is needed so the presigned upload form (signed with this role) works.
  # GetObject covers HeadObject; DeleteObject removes rejected uploads.
  statement {
    sid       = "InputObjects"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.images["input"].arn}/uploads/*"]
  }

  # Needed to sign presigned download URLs.
  # (A presigned URL only works if the signer itself has permission for the action.)
  statement {
    sid       = "OutputRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images["output"].arn}/results/*"]
  }
}

# Attach the policy above to the role as an inline policy.
resource "aws_iam_role_policy" "job_api" {
  name   = "job-api-least-privilege"
  role   = aws_iam_role.job_api.id
  policy = data.aws_iam_policy_document.job_api.json
}

# ---------------------------------------------------------------- processor

# Role used by the image-processing Lambda.
resource "aws_iam_role" "processor" {
  name               = "${local.name}-processor-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# The processor can read uploads, write results, update jobs and publish to SNS -
# nothing else. It cannot create jobs or read results.
data "aws_iam_policy_document" "processor" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.processor.arn}:*"]
  }

  # Read a job and update its status/progress/results (no PutItem: it never creates jobs).
  statement {
    sid       = "JobsTable"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.jobs.arn]
  }

  # Download the original image.
  statement {
    sid       = "ReadUploads"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images["input"].arn}/uploads/*"]
  }

  # Save the processed images.
  statement {
    sid       = "WriteResults"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.images["output"].arn}/results/*"]
  }

  # Send completion/failure notifications to this one topic only.
  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notifications.arn]
  }
}

resource "aws_iam_role_policy" "processor" {
  name   = "processor-least-privilege"
  role   = aws_iam_role.processor.id
  policy = data.aws_iam_policy_document.processor.json
}
