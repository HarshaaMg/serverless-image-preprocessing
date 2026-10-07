# Two private buckets: input (uploads) and output (results). Neither is publicly
# readable. Browsers reach them only via short-lived presigned URLs.
# (The React frontend runs locally; there is no frontend bucket.)

# The two buckets and the key prefix each one uses. The Lambda code writes
# uploads/<jobId>/original to "input" and results/<jobId>/<variant>.<ext> to "output".
locals {
  image_buckets = {
    input  = { name = local.input_bucket_name, prefix = "uploads/" }
    output = { name = local.output_bucket_name, prefix = "results/" }
  }
}

# INTERVIEW:
# `for_each` creates one resource per map entry - here two buckets, addressed in
# Terraform as aws_s3_bucket.images["input"] and aws_s3_bucket.images["output"].
# Keying by name (not by position) means removing one entry never renumbers or
# recreates the others.
resource "aws_s3_bucket" "images" {
  for_each      = local.image_buckets
  bucket        = each.value.name
  force_destroy = false # buckets must be emptied deliberately before destroy
}

locals {
  # Kept under this name so existing resource addresses (".all[...]") do not change.
  all_buckets = aws_s3_bucket.images
}

# INTERVIEW:
# "Block Public Access" is the account/bucket-level safety switch that prevents the
# bucket from EVER being made public, even by a mistaken ACL or bucket policy. All four
# settings are on. Users reach objects only through presigned URLs.
resource "aws_s3_bucket_public_access_block" "all" {
  for_each                = local.all_buckets
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Disable old-style ACLs completely; access is controlled only by IAM and bucket policies.
resource "aws_s3_bucket_ownership_controls" "all" {
  for_each = local.all_buckets
  bucket   = each.value.id
  rule {
    object_ownership = "BucketOwnerEnforced" # disables ACLs entirely
  }
}

# Encrypt every object at rest with S3-managed keys (SSE-S3).
resource "aws_s3_bucket_server_side_encryption_configuration" "all" {
  for_each = local.all_buckets
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3, no extra cost
    }
  }
}

# Versioning keeps old copies of overwritten objects. Off by default (see variables.tf).
resource "aws_s3_bucket_versioning" "images" {
  for_each = aws_s3_bucket.images
  bucket   = each.value.id
  versioning_configuration {
    status = var.enable_versioning ? "Enabled" : "Disabled"
  }
}

# INTERVIEW:
# Lifecycle rules make S3 delete objects automatically, which controls storage cost and
# keeps user images for a limited time only (privacy). S3 applies them in the
# background, so deletion happens some time after the age is reached, not instantly.
resource "aws_s3_bucket_lifecycle_configuration" "images" {
  for_each = aws_s3_bucket.images
  bucket   = each.value.id

  # Delete images (and old versions, if versioning is on) after file_retention_days.
  rule {
    id     = "expire-images"
    status = "Enabled"
    filter {
      prefix = local.image_buckets[each.key].prefix
    }
    expiration {
      days = var.file_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.file_retention_days
    }
  }

  # Clean up parts of uploads that were started but never finished.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  # INTERVIEW:
  # Terraform works out the order of operations from references between resources.
  # `depends_on` adds an ordering rule Terraform cannot infer by itself.
  depends_on = [aws_s3_bucket_versioning.images]
}

# INTERVIEW:
# CORS (Cross-Origin Resource Sharing): browsers block a page from one origin
# (http://localhost:5173) reading responses from another origin (the S3 bucket) unless
# that server allows it. These rules allow ONLY our app's origin and ONLY the methods it
# needs. CORS restricts browsers; it is not authentication - presigned URLs and job
# tokens are what actually protect the data.
# Browser access: uploads (POST) to input, previews/downloads (GET) from output.
resource "aws_s3_bucket_cors_configuration" "input" {
  bucket = aws_s3_bucket.images["input"].id
  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = local.cors_origins
    allowed_headers = ["content-type"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_cors_configuration" "output" {
  bucket = aws_s3_bucket.images["output"].id
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = local.cors_origins
    allowed_headers = []
    max_age_seconds = 3000
  }
}

# Bucket policies: refuse any non-HTTPS request.
# aws_iam_policy_document is a data source that builds policy JSON from HCL, which is
# easier to read and validate than hand-written JSON strings.
data "aws_iam_policy_document" "tls_only" {
  for_each = aws_s3_bucket.images

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [each.value.arn, "${each.value.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# Attach the HTTPS-only policy to each bucket (after Block Public Access is in place).
resource "aws_s3_bucket_policy" "images" {
  for_each   = aws_s3_bucket.images
  bucket     = each.value.id
  policy     = data.aws_iam_policy_document.tls_only[each.key].json
  depends_on = [aws_s3_bucket_public_access_block.all]
}

# INTERVIEW:
# S3 event notification = the "trigger" of this event-driven system. Whenever an
# object whose key starts with uploads/ and ends with /original is created, S3 invokes
# the processor Lambda asynchronously with the bucket name and object key.
# S3 -> processing Lambda trigger
resource "aws_s3_bucket_notification" "input" {
  bucket = aws_s3_bucket.images["input"].id

  lambda_function {
    lambda_function_arn = aws_lambda_function.processor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "uploads/"
    filter_suffix       = "/original"
  }

  # S3 checks that it is allowed to invoke the Lambda when the notification is saved,
  # so the permission (lambda.tf) must exist first.
  depends_on = [aws_lambda_permission.s3_invoke_processor]
}
