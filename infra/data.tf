# DynamoDB job table and SNS notifications.

# INTERVIEW:
# DynamoDB is a serverless NoSQL key-value database. Each item is one job, found by its
# partition key `jobId` (a UUID). We only ever look jobs up by ID, so no indexes or
# table scans are needed. The Lambdas use conditional writes on this table to manage
# job status safely (see backend/src/imgproc/jobs.py).
resource "aws_dynamodb_table" "jobs" {
  name = "${local.name}-jobs"
  # On-demand billing: no servers or capacity to manage; you pay per read/write.
  billing_mode = "PAY_PER_REQUEST" # pay only for requests; no idle cost
  hash_key     = "jobId"

  # Only key attributes are declared up front; other attributes (status, outputs...)
  # can be added freely to each item because DynamoDB is schemaless.
  attribute {
    name = "jobId"
    type = "S"
  }

  # INTERVIEW:
  # TTL (Time To Live): DynamoDB deletes an item for free some time after the Unix
  # timestamp stored in `expiresAt` has passed. The job API sets expiresAt to
  # "created + 1 day", so old jobs clean themselves up.
  # Job records are deleted automatically (usually within a few days of expiresAt).
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  # Encryption at rest is always on (AWS owned key, free).
}

# INTERVIEW:
# SNS (Simple Notification Service) topic: the processor Lambda publishes one message
# per finished/failed job, and SNS fans it out to every subscriber. The publisher does
# not need to know who the subscribers are (loose coupling).
resource "aws_sns_topic" "notifications" {
  name = "${local.name}-job-notifications"
}

# Email subscription. `count = 0 or 1` is the Terraform way to create a resource only
# when a condition is true (here: an email address was provided). AWS sends a
# confirmation email first; nothing is delivered until the link in it is clicked.
resource "aws_sns_topic_subscription" "email" {
  count     = var.notification_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}
