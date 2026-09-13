# scripts/build.sh must run before plan/apply. `make plan` and `make deploy`
# enforce that ordering.
#
# There is no Lambda layer: scripts/build.sh puts the runtime dependencies
# (boto3, requests, tzdata and their transitive ones) into the same zip as the
# application source. They are packaged rather than taken from the runtime so
# the function pins its own versions, and a single artifact means there is no
# second one to version and deploy in step.

resource "aws_lambda_function" "grabber" {
  function_name = local.name
  role          = aws_iam_role.lambda.arn
  handler       = "entsoe_grabber.handler.handler"
  runtime       = "python3.14"
  architectures = [var.lambda_architecture]

  filename = local.function_zip
  # Without this, Terraform sees no change when only the code changes and
  # silently skips the redeploy.
  source_code_hash = filebase64sha256(local.function_zip)

  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout_seconds

  # ENTSO-E applies its request budget per token rather than per host. Keep
  # scheduled and manual invocations from spending that shared budget in
  # parallel; requests within each invocation are made sequentially.
  reserved_concurrent_executions = 1

  # Private subnet only. Egress to ENTSO-E goes out through fck-nat; S3 goes
  # through the gateway endpoint.
  vpc_config {
    subnet_ids         = [data.aws_subnet.private.id]
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      OUTPUT_BUCKET              = data.aws_s3_bucket.data.id
      OUTPUT_PREFIX              = var.output_prefix
      ENTSOE_TOKEN_SSM_PARAMETER = local.token_parameter_name
      ENTSOE_BASE_URL            = var.entsoe_base_url
      DATASETS_JSON              = jsonencode(var.datasets)
      MARKET_TIMEZONE            = var.market_timezone
      STORE_RAW_XML              = tostring(var.store_raw_xml)
      LOG_LEVEL                  = "INFO"
    }
  }

  depends_on = [
    # Ensure the log group (with our retention) exists before Lambda creates one
    # with infinite retention.
    aws_cloudwatch_log_group.lambda,
    # Creating a VPC-attached function immediately calls CreateNetworkInterface.
    # Terraform cannot infer that the inline policy must exist first.
    aws_iam_role_policy.lambda,
  ]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

# EventBridge invokes Lambda asynchronously. Capture execution failures after
# Lambda exhausts retries, including timeouts and events that age out.
resource "aws_lambda_function_event_invoke_config" "grabber" {
  function_name                = aws_lambda_function.grabber.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 21600

  destination_config {
    on_failure {
      destination = aws_sqs_queue.lambda_failures.arn
    }
  }
}

# --- schedule ----------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${local.name}-schedule"
  description         = "Trigger the ENTSO-E grabber"
  schedule_expression = var.schedule_expression
}

# Pass the original event, including its time, so retries keep the intended day.
resource "aws_cloudwatch_event_target" "lambda" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "lambda"
  arn       = aws_lambda_function.grabber.arn

  depends_on = [
    aws_lambda_permission.events,
    aws_lambda_function_event_invoke_config.grabber,
  ]
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.grabber.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
