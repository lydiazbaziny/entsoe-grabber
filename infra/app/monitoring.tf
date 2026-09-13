# Lambda writes a full invocation record here, including the original event
# and error details. This is an on-failure destination, not an event source:
# recovery is manual after investigating the failure.
resource "aws_sqs_queue" "lambda_failures" {
  name                      = "${local.name}-lambda-failures"
  message_retention_seconds = 1209600 # 14 days, the SQS maximum.
  sqs_managed_sse_enabled   = true
}

resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  for_each = var.alarm_email_addresses

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

locals {
  lambda_alarms = {
    errors = {
      description = "A grabber invocation failed. Check Lambda logs; asynchronous invocations may still retry."
      metric      = "Errors"
      statistic   = "Sum"
      threshold   = 1
    }
    destination_delivery_failures = {
      description = "Lambda could not retain a failed invocation in SQS. Check destination permissions and message size."
      metric      = "DestinationDeliveryFailures"
      statistic   = "Sum"
      threshold   = 1
    }
    duration = {
      description = "A grabber invocation used at least 80% of its timeout. Check API latency and the dataset count."
      metric      = "Duration"
      statistic   = "Maximum"
      threshold   = var.lambda_timeout_seconds * 1000 * 0.8
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda" {
  for_each = local.lambda_alarms

  alarm_name        = "${local.name}-${each.key}"
  alarm_description = each.value.description
  namespace         = "AWS/Lambda"
  metric_name       = each.value.metric
  statistic         = each.value.statistic
  # Lambda timestamps metrics at invocation start but emits them after the run.
  # Keep one bad minute visible across a full timeout plus reporting allowance.
  period              = 60
  evaluation_periods  = ceil(var.lambda_timeout_seconds / 60) + 5
  datapoints_to_alarm = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = each.value.threshold

  dimensions = { FunctionName = aws_lambda_function.grabber.function_name }

  # A daily function has no samples for most periods; silence is expected.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alarms.arn]

  depends_on = [aws_sns_topic_policy.alarms]
}

resource "aws_cloudwatch_metric_alarm" "lambda_failures" {
  alarm_name          = "${local.name}-queued-failures"
  alarm_description   = "A failed asynchronous invocation needs investigation. Replay its requestPayload after fixing the cause."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1

  dimensions = { QueueName = aws_sqs_queue.lambda_failures.name }

  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alarms.arn]

  depends_on = [aws_sns_topic_policy.alarms]
}
