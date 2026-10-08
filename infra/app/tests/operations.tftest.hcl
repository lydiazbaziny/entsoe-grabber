# All AWS operations are mocked, including the lookups of the network and
# storage stacks' resources; these tests never deploy.
mock_provider "aws" {
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "eu-north-1", name = "eu-north-1" }
  }
  # Mock the computed JSON only. Security assertions below inspect the actual
  # statement inputs; they do not rely on this placeholder policy.
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/entsoe-grabber-dev-lambda" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:eu-north-1:123456789012:entsoe-grabber-dev-lambda-failures" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:eu-north-1:123456789012:entsoe-grabber-dev-alarms" }
  }
}

variables {
  datasets = {
    forecast = { params = { documentType = "A71" }, date_offset_days = 1 }
  }
}

run "retain_failed_invocations" {
  command = plan

  assert {
    condition = (
      aws_lambda_function_event_invoke_config.grabber.function_name == aws_lambda_function.grabber.function_name &&
      aws_lambda_function_event_invoke_config.grabber.qualifier == null &&
      aws_lambda_function_event_invoke_config.grabber.destination_config[0].on_failure[0].destination == aws_sqs_queue.lambda_failures.arn &&
      aws_lambda_function_event_invoke_config.grabber.maximum_retry_attempts == 2 &&
      aws_lambda_function_event_invoke_config.grabber.maximum_event_age_in_seconds == 21600
    )
    error_message = "Unqualified asynchronous invocations must retain exhausted failures in SQS with the existing retry allowance."
  }

  assert {
    condition = (
      aws_sqs_queue.lambda_failures.message_retention_seconds == 1209600 &&
      aws_sqs_queue.lambda_failures.sqs_managed_sse_enabled &&
      aws_sqs_queue.lambda_failures.fifo_queue != true
    )
    error_message = "Failure records must use a standard encrypted queue with 14-day retention."
  }

  assert {
    condition = one([
      for statement in data.aws_iam_policy_document.lambda.statement :
      statement.effect == "Allow" &&
      statement.actions == toset(["sqs:SendMessage"]) &&
      statement.resources == toset([aws_sqs_queue.lambda_failures.arn])
      if statement.sid == "RetainFailedInvocations"
    ])
    error_message = "Lambda must have SendMessage permission scoped to the failure queue."
  }
}

run "restrict_function_ec2_calls" {
  command = plan

  # A renamed function must remain protected, with no dependency on its ARN
  # output (which would introduce an IAM-policy/function dependency cycle).
  variables {
    project_name = "custom-grabber"
    environment  = "qa"
    aws_region   = "us-west-2"
  }

  assert {
    condition = one([
      for statement in data.aws_iam_policy_document.lambda.statement :
      statement.effect == "Deny" &&
      statement.resources == toset(["*"]) &&
      statement.actions == toset([
        "ec2:AssignPrivateIpAddresses", "ec2:CreateNetworkInterface",
        "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSubnets", "ec2:DetachNetworkInterface",
        "ec2:UnassignPrivateIpAddresses",
      ]) &&
      length(statement.condition) == 1 &&
      one(statement.condition).test == "ArnEquals" &&
      one(statement.condition).variable == "lambda:SourceFunctionArn" &&
      toset(one(statement.condition).values) == toset(["arn:aws:lambda:us-west-2:123456789012:function:custom-grabber-qa"])
      if statement.sid == "DenyVpcManagementFromFunctionCode"
    ])
    error_message = "Deny ENI management only for calls originating from this function's code, using its unqualified ARN."
  }

  assert {
    condition = one([
      for statement in data.aws_iam_policy_document.lambda.statement :
      statement.effect == "Allow" &&
      statement.resources == toset(["*"]) &&
      length(statement.condition) == 0 &&
      statement.actions == toset([
        "ec2:AssignPrivateIpAddresses", "ec2:CreateNetworkInterface",
        "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSubnets", "ec2:UnassignPrivateIpAddresses",
      ])
      if statement.sid == "ManageVpcNetworkInterfaces"
    ])
    error_message = "Keep the Lambda service's required ENI permissions."
  }
}

run "notify_on_operational_failures" {
  command = plan

  variables {
    lambda_timeout_seconds = 300
    alarm_email_addresses  = ["oncall@example.com"]
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.lambda["errors"].metric_name == "Errors" &&
      aws_cloudwatch_metric_alarm.lambda["errors"].statistic == "Sum" &&
      aws_cloudwatch_metric_alarm.lambda["errors"].threshold == 1 &&
      aws_cloudwatch_metric_alarm.lambda["destination_delivery_failures"].metric_name == "DestinationDeliveryFailures" &&
      aws_cloudwatch_metric_alarm.lambda["destination_delivery_failures"].statistic == "Sum" &&
      aws_cloudwatch_metric_alarm.lambda["destination_delivery_failures"].threshold == 1 &&
      aws_cloudwatch_metric_alarm.lambda["duration"].metric_name == "Duration" &&
      aws_cloudwatch_metric_alarm.lambda["duration"].statistic == "Maximum" &&
      aws_cloudwatch_metric_alarm.lambda["duration"].threshold == 240000
    )
    error_message = "Detect individual invocation/destination errors and warn at 80% of the configured timeout in milliseconds."
  }

  assert {
    condition = alltrue([
      for alarm in aws_cloudwatch_metric_alarm.lambda :
      alarm.namespace == "AWS/Lambda" &&
      alarm.dimensions == tomap({ FunctionName = aws_lambda_function.grabber.function_name }) &&
      alarm.period == 60 && alarm.evaluation_periods == 10 && alarm.datapoints_to_alarm == 1 &&
      alarm.comparison_operator == "GreaterThanOrEqualToThreshold" &&
      alarm.treat_missing_data == "notBreaching" &&
      alarm.alarm_actions == toset([aws_sns_topic.alarms.arn])
    ])
    error_message = "Lambda alarms must notify SNS for one bad minute, allow late metrics beyond the timeout, and tolerate normal daily gaps."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.lambda_failures.namespace == "AWS/SQS" &&
      aws_cloudwatch_metric_alarm.lambda_failures.metric_name == "ApproximateNumberOfMessagesVisible" &&
      aws_cloudwatch_metric_alarm.lambda_failures.dimensions == tomap({ QueueName = aws_sqs_queue.lambda_failures.name }) &&
      aws_cloudwatch_metric_alarm.lambda_failures.statistic == "Maximum" &&
      aws_cloudwatch_metric_alarm.lambda_failures.threshold == 1 &&
      aws_cloudwatch_metric_alarm.lambda_failures.comparison_operator == "GreaterThanOrEqualToThreshold" &&
      aws_cloudwatch_metric_alarm.lambda_failures.treat_missing_data == "notBreaching" &&
      aws_cloudwatch_metric_alarm.lambda_failures.alarm_actions == toset([aws_sns_topic.alarms.arn])
    )
    error_message = "A queued failure must notify SNS for manual recovery."
  }

  assert {
    condition = (
      aws_sns_topic_subscription.alarm_email["oncall@example.com"].topic_arn == aws_sns_topic.alarms.arn &&
      aws_sns_topic_subscription.alarm_email["oncall@example.com"].protocol == "email" &&
      aws_sns_topic_subscription.alarm_email["oncall@example.com"].endpoint == "oncall@example.com"
    )
    error_message = "Configured email recipients must subscribe to the alarms topic."
  }

  assert {
    condition = one([
      for statement in data.aws_iam_policy_document.alarms.statement :
      statement.effect == "Allow" &&
      statement.actions == toset(["sns:Publish"]) &&
      statement.resources == toset([aws_sns_topic.alarms.arn]) &&
      one(statement.principals).type == "Service" &&
      one(statement.principals).identifiers == toset(["cloudwatch.amazonaws.com"]) &&
      length(statement.condition) == 2 &&
      one([for c in statement.condition : c.test == "StringEquals" && toset(c.values) == toset(["123456789012"]) if c.variable == "aws:SourceAccount"]) &&
      one([for c in statement.condition : c.test == "ArnLike" && toset(c.values) == toset(["arn:aws:cloudwatch:eu-north-1:123456789012:alarm:entsoe-grabber-dev-*"]) if c.variable == "aws:SourceArn"])
      if statement.sid == "PublishCloudWatchAlarms"
    ])
    error_message = "Only this deployment's CloudWatch alarms should receive service permission to publish to the topic."
  }
}
