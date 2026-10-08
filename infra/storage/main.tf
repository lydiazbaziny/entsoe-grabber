# Stateful resources live in their own stack, so applying or destroying the app
# stack can never touch the collected data or the manually written token. The
# app stack derives both names the same way; keep them in step.

locals {
  name = "${var.project_name}-${var.environment}"
}

data "aws_caller_identity" "current" {}

# --- output bucket -----------------------------------------------------------

resource "aws_s3_bucket" "data" {
  bucket = "${local.name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

# --- API token ---------------------------------------------------------------
# Terraform creates the parameter with a write-only placeholder. The real token
# is written with the AWS CLI after deployment and is never stored in state.

resource "aws_ssm_parameter" "entsoe_token" {
  name             = "/${var.project_name}/${var.environment}/api-token"
  description      = "ENTSO-E Transparency Platform API token"
  type             = "SecureString"
  value_wo         = "replace-me-after-deployment"
  value_wo_version = 1
}
