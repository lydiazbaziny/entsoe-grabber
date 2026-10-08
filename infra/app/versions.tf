terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # bucket and region come from infra/backend.hcl; see backend.hcl.example.
  backend "s3" {
    key          = "entsoe-grabber/dev/app.tfstate"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.62"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
