# A deliberately small, single-AZ VPC: fck-nat in a public subnet and the Lambda
# in a private subnet. This satisfies the assignment without production-grade
# multi-AZ complexity.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # The app stack looks up the VPC and private subnet by these Name tags.
  name = "${var.project_name}-${var.environment}"

  # The names list is sorted, so this does not shuffle between plans.
  az = data.aws_availability_zones.available.names[0]
}

resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  tags = { Name = local.name }
}

# --- public: the NAT instance and the way out --------------------------------

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.name }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  availability_zone = local.az
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 0)

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --- private: where the Lambda runs ------------------------------------------

resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.main.id
  availability_zone = local.az
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10)

  tags = { Name = "${local.name}-private" }
}

# No default route here. fck-nat writes 0.0.0.0/0 into this table itself, and
# declaring it in both places would make Terraform and the module overwrite
# each other on every apply.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

# --- NAT ---------------------------------------------------------------------

# In this module, HA mode is a self-healing single-instance Auto Scaling Group;
# it does not add another NAT instance or Availability Zone.
module "fck_nat" {
  source  = "RaJiska/fck-nat/aws"
  version = "~> 1.6.0"

  name          = "${local.name}-nat"
  vpc_id        = aws_vpc.main.id
  subnet_id     = aws_subnet.public.id
  instance_type = var.nat_instance_type
  ha_mode       = true

  # Refresh the running instance when its launch template changes. Replacing
  # our single NAT instance briefly interrupts outbound internet access.
  auto_rollout = true

  update_route_tables = true
  route_tables_ids    = { private = aws_route_table.private.id }
}

# --- S3 without touching the NAT ---------------------------------------------

# A gateway endpoint is free and keeps the CSV uploads off the t4g.micro's
# network path. Without it, every byte written to S3 would pass through the
# NAT instance.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-s3" }
}
