# The VPC and private subnet belong to the network stack and are found by their
# Name tags. The plan fails here if that stack has not been deployed.

data "aws_vpc" "main" {
  tags = { Name = local.name }
}

data "aws_subnet" "private" {
  vpc_id = data.aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

# --- Lambda security group ---------------------------------------------------

# No ingress: nothing connects to a Lambda ENI. HTTPS egress reaches ENTSO-E,
# SSM, and S3 while the S3 gateway endpoint keeps uploads off the NAT path.
resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda"
  description = "Egress-only group for the grabber Lambda"
  vpc_id      = data.aws_vpc.main.id

  tags = { Name = "${local.name}-lambda" }
}

resource "aws_vpc_security_group_egress_rule" "lambda_https" {
  security_group_id = aws_security_group.lambda.id
  description       = "HTTPS to ENTSO-E and AWS APIs"

  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 443
  ip_protocol = "tcp"
  to_port     = 443
}
