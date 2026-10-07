# API Gateway HTTP API -> job-management Lambda.

# INTERVIEW:
# API Gateway is the managed "front door" for the backend: it gives us a public HTTPS
# URL, routes requests to Lambda, handles CORS and throttling, and scales automatically.
# An "HTTP API" (v2) is the simpler, cheaper flavour compared with a "REST API" (v1).
resource "aws_apigatewayv2_api" "http" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"

  # CORS for the browser app. API Gateway answers the browser's preflight OPTIONS
  # request itself and adds Access-Control-* headers to responses, so the Lambda code
  # does not deal with CORS at all. x-job-token must be listed because it is a custom
  # header the frontend sends.
  cors_configuration {
    allow_origins = local.cors_origins
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type", "x-job-token"]
    max_age       = 3600
  }
}

# INTERVIEW:
# AWS_PROXY ("Lambda proxy integration") forwards the whole HTTP request to Lambda as an
# event and returns whatever the function returns (statusCode, headers, body).
# Payload format 2.0 is the event shape job_api.handler expects (event["routeKey"]...).
resource "aws_apigatewayv2_integration" "job_api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.job_api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

# The four routes of the API. These strings must match the ROUTES dict in
# backend/src/imgproc/job_api.py exactly.
locals {
  api_routes = [
    "POST /api/jobs",
    "POST /api/jobs/{jobId}/complete-upload",
    "GET /api/jobs/{jobId}",
    "GET /api/jobs/{jobId}/results",
  ]
}

# One route resource per entry; all of them send requests to the same Lambda.
resource "aws_apigatewayv2_route" "routes" {
  for_each  = toset(local.api_routes)
  api_id    = aws_apigatewayv2_api.http.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.job_api.id}"
}

# Access logs: one line per HTTP request (status code, latency...), kept for 14 days.
resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${local.name}-api"
  retention_in_days = var.log_retention_days
}

# A "stage" is a deployed version of the API. "$default" with auto_deploy means every
# change is live immediately at the base URL (no /prod or /dev path segment).
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  # INTERVIEW:
  # Throttling limits requests per second (steady rate + short bursts); extra requests
  # get HTTP 429 "Too Many Requests". With no login system, this caps abuse and cost.
  default_route_settings {
    throttling_rate_limit  = var.api_throttle_rate
    throttling_burst_limit = var.api_throttle_burst
  }

  # Access log fields deliberately exclude headers (they contain the job token).
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      ip               = "$context.identity.sourceIp"
      requestTime      = "$context.requestTime"
      routeKey         = "$context.routeKey"
      status           = "$context.status"
      responseLength   = "$context.responseLength"
      latencyMs        = "$context.responseLatency"
      integrationError = "$context.integrationErrorMessage"
    })
  }
}

# Allow API Gateway (this API only, any stage/route) to invoke the job-API Lambda.
resource "aws_lambda_permission" "api_invoke_job_api" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.job_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
