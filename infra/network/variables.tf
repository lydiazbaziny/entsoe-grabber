# aws_region, project_name and environment must match the storage and app
# stacks: app finds this stack's VPC and subnet by the names they produce.

variable "aws_region" {
  description = "Region to deploy into. ENTSO-E data is European, so an eu-* region keeps latency and egress cost low."
  type        = string
  default     = "eu-north-1"
}

variable "project_name" {
  description = "Prefix for all resource names."
  type        = string
  default     = "entsoe-grabber"
}

variable "environment" {
  description = "Deployment environment (dev/staging/prod)."
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR for the VPC. A /16 leaves room for the /24 subnets taken from it."
  type        = string
  default     = "10.0.0.0/16"
}

variable "nat_instance_type" {
  description = <<-EOT
    Instance type for the fck-nat NAT instance. t4g.micro is the smallest
    Graviton type eligible for the AWS Free Tier: accounts on the Free plan
    reject t4g.nano with "not eligible for Free Tier". List the eligible types
    with `aws ec2 describe-instance-types --filters
    Name=free-tier-eligible,Values=true`.
  EOT
  type        = string
  default     = "t4g.micro"
}
