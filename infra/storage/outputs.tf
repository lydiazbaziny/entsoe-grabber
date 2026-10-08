output "data_bucket" {
  description = "Bucket the grabber writes to."
  value       = aws_s3_bucket.data.id
}

output "token_parameter_name" {
  description = "Populate with: aws ssm put-parameter --name <this> --type SecureString --value <token> --overwrite"
  value       = aws_ssm_parameter.entsoe_token.name
}
