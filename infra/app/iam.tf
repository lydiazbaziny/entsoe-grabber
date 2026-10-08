data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid    = "WriteLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid    = "WriteData"
    effect = "Allow"

    actions   = ["s3:PutObject"]
    resources = ["${data.aws_s3_bucket.data.arn}/*"]
  }

  statement {
    sid    = "ReadApiToken"
    effect = "Allow"

    # No kms:Decrypt alongside this. The parameter is a SecureString under the
    # AWS managed key `aws/ssm`, whose key policy already permits the account's
    # principals to decrypt through Systems Manager -- AWS documents that you
    # "cannot establish access control policies for the default aws/ssm KMS
    # key". Adding the permission here would grant nothing. Setting `key_id` on
    # the parameter to a customer managed key would change that: the role would
    # then need kms:Decrypt on that key's ARN.
    actions   = ["ssm:GetParameter"]
    resources = [local.token_parameter_arn]
  }

  statement {
    sid    = "RetainFailedInvocations"
    effect = "Allow"

    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.lambda_failures.arn]
  }

  statement {
    sid    = "ManageVpcNetworkInterfaces"
    effect = "Allow"

    # Lambda needs these permissions to attach the function to a VPC. EC2 does
    # not support resource-level permissions for all of these actions.
    actions = [
      "ec2:AssignPrivateIpAddresses",
      "ec2:CreateNetworkInterface",
      "ec2:DeleteNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:UnassignPrivateIpAddresses",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DenyVpcManagementFromFunctionCode"
    effect = "Deny"

    actions = [
      "ec2:AssignPrivateIpAddresses",
      "ec2:CreateNetworkInterface",
      "ec2:DeleteNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:DetachNetworkInterface",
      "ec2:UnassignPrivateIpAddresses",
    ]
    resources = ["*"]

    # This key is present for calls made by function code, but not Lambda's
    # own ENI management. Construct the unqualified ARN to avoid a dependency
    # cycle: the function itself already depends on this policy.
    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = ["arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.name}"]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

data "aws_iam_policy_document" "alarms" {
  statement {
    sid       = "PublishCloudWatchAlarms"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name}-*"]
    }
  }
}

resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarms.json
}
