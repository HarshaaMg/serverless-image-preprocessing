# INTERVIEW:
# Input variables make the configuration reusable without editing code. Each has a type,
# a description and usually a default. Values can be overridden in terraform.tfvars
# (git-ignored; this project sets aws_region, project_name and notification_email there)
# or on the command line. Referenced as var.<name>. `validation` blocks reject bad
# values during `terraform plan`, before anything is created.

# Region for every regional resource (S3, Lambda, DynamoDB, API Gateway, SNS, logs).
variable "aws_region" {
  description = "AWS region for all regional resources."
  type        = string
  default     = "ap-south-1"
}

# Prefix for resource names (imgprep-jobs, imgprep-processor, ...).
variable "project_name" {
  description = "Short lowercase name used as a prefix for resource names."
  type        = string
  default     = "imgprep"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project_name))
    error_message = "Use 3-21 lowercase letters, digits or hyphens, starting with a letter."
  }
}

# Email address that receives SNS job notifications ("" = no subscription is created).
variable "notification_email" {
  description = "Email address for SNS job notifications. Leave empty to skip the subscription."
  type        = string
  default     = ""
}

# Origins (scheme + host + port) the browser app is served from. Used for CORS on the
# API (api.tf) and on both buckets (s3.tf). Default: the local Vite dev server.
variable "cors_allowed_origins" {
  description = "Browser origins allowed to call the API, upload to the input bucket and preview from the output bucket."
  type        = list(string)
  default     = ["http://localhost:5173"]

  validation {
    condition     = length(var.cors_allowed_origins) > 0 && alltrue([for o in var.cors_allowed_origins : can(regex("^https?://[^/*]+$", o))])
    error_message = "Use full origins such as http://localhost:5173 (no paths, no wildcards)."
  }
}

# How long images stay in S3 (lifecycle rule) and job records in DynamoDB (TTL).
variable "file_retention_days" {
  description = "Days before uploaded and processed images are deleted automatically."
  type        = number
  default     = 1
}

# How long CloudWatch keeps log lines (logs cost money to store, so they expire).
variable "log_retention_days" {
  description = "CloudWatch Logs retention in days."
  type        = number
  default     = 14
}

# Optional S3 versioning (off by default: results are short-lived and regenerable).
variable "enable_versioning" {
  description = "Enable S3 versioning on the image buckets (old versions expire after file_retention_days)."
  type        = bool
  default     = false
}

# Processor Lambda sizing. In Lambda, CPU power scales with the memory setting.
variable "processor_memory_mb" {
  description = "Memory for the image-processing Lambda (more memory also means more CPU)."
  type        = number
  default     = 1536
}

variable "processor_timeout_seconds" {
  description = "Timeout for the image-processing Lambda."
  type        = number
  default     = 60
}

variable "processor_reserved_concurrency" {
  description = "Max concurrent image-processing Lambdas. -1 = no reservation (accounts with a low concurrency quota cannot reserve)."
  type        = number
  default     = -1
}

# API Gateway throttling: protects against abuse and runaway cost (there is no login).
variable "api_throttle_rate" {
  description = "Steady-state API requests per second."
  type        = number
  default     = 10
}

variable "api_throttle_burst" {
  description = "API burst limit."
  type        = number
  default     = 20
}
