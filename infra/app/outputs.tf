output "function_name" {
  description = "Invoke with: aws lambda invoke --function-name <this> out.json"
  value       = aws_lambda_function.grabber.function_name
}

output "log_group" {
  description = "Tail with: aws logs tail <this> --follow"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "failure_queue_url" {
  description = "SQS queue containing failed asynchronous invocation records for investigation and manual recovery."
  value       = aws_sqs_queue.lambda_failures.url
}

output "alarm_topic_arn" {
  description = "SNS alarm topic. Configure alarm_email_addresses or subscribe another notification consumer."
  value       = aws_sns_topic.alarms.arn
}
