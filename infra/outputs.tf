# INTERVIEW:
# Outputs print useful values after `terraform apply` and can be read later with
# `terraform output <name>`. Scripts use them instead of hard-coding IDs: for example
# scripts/configure_frontend.ps1 reads api_endpoint and writes it into the React
# app's .env.aws.local file.

# The public base URL of the HTTP API, e.g. https://xxxx.execute-api.ap-south-1.amazonaws.com
output "api_endpoint" {
  description = "API Gateway base URL (used as VITE_API_BASE_URL)."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

# Bucket and table names - handy for inspecting data with the AWS CLI and for cleanup.
output "input_bucket" {
  value = aws_s3_bucket.images["input"].id
}

output "output_bucket" {
  value = aws_s3_bucket.images["output"].id
}

output "jobs_table" {
  value = aws_dynamodb_table.jobs.name
}

output "sns_topic_arn" {
  value = aws_sns_topic.notifications.arn
}

# Lambda names, e.g. for `aws logs tail /aws/lambda/<name>`.
output "job_api_function" {
  value = aws_lambda_function.job_api.function_name
}

output "processor_function" {
  value = aws_lambda_function.processor.function_name
}

output "aws_region" {
  value = var.aws_region
}

output "cors_allowed_origins" {
  description = "Origins the browser app may be served from."
  value       = local.cors_origins
}
