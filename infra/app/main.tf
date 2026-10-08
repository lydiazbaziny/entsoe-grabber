locals {
  name = "${var.project_name}-${var.environment}"

  build_dir    = "${path.module}/../../build"
  function_zip = "${local.build_dir}/function.zip"

  # Created by the storage stack. The name and ARN are built here rather than
  # read with the aws_ssm_parameter data source, which would copy the decrypted
  # token into this stack's state.
  token_parameter_name = "/${var.project_name}/${var.environment}/api-token"
  token_parameter_arn  = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.token_parameter_name}"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# Created by the storage stack. Looking it up fails the plan early if that
# stack has not been deployed.
data "aws_s3_bucket" "data" {
  bucket = "${local.name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}
