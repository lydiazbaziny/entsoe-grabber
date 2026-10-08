# aws_region, project_name and environment must match the network and app
# stacks: app finds this stack's bucket and token parameter by the names they
# produce.

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
