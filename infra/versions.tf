# INTERVIEW:
# Terraform is Infrastructure as Code (IaC): instead of clicking resources together in
# the AWS console, we DESCRIBE the desired infrastructure in .tf files. `terraform plan`
# shows what would change and `terraform apply` makes AWS match the files. The files
# are version-controlled, reviewable and repeatable, and every resource in this project
# was created this way. Terraform loads all .tf files in this folder as one config;
# the split into files (s3.tf, lambda.tf, ...) is only for readability.

# Which Terraform version and which providers (plugins) this project needs.
# `terraform init` downloads the providers and records exact versions in
# .terraform.lock.hcl, so everyone gets the same versions.
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    # Creates and manages AWS resources.
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    # Zips the Lambda code folders (see data "archive_file" in lambda.tf).
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
    # Generates the random suffix that makes bucket names globally unique (main.tf).
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # INTERVIEW:
  # Terraform "state" is a JSON file mapping each resource in the code to the real AWS
  # object it created (IDs, ARNs...). Without it Terraform cannot update or destroy
  # what it made. It can contain sensitive details, so it is git-ignored and backed up
  # manually (infra/state-backups/). Teams usually store it remotely (e.g. in S3).
  # State is stored locally in infra/terraform.tfstate (git-ignored).
  # Keep it safe: Terraform needs it to update or destroy these resources later.
}

# Configure the AWS provider. No credentials are written here: Terraform uses the
# same credentials as the AWS CLI on this machine (`aws configure`).
provider "aws" {
  # The region comes from a variable (default ap-south-1, Mumbai) - see variables.tf.
  region = var.aws_region

  # These tags are added to every AWS resource automatically, which makes it easy to
  # find (and cost-track) everything belonging to this project in the console.
  default_tags {
    tags = {
      Project   = "ImagePreprocessing"
      ManagedBy = "Terraform"
    }
  }
}
