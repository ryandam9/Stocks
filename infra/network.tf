# A dedicated VPC. Public subnets with egress-only security groups rather than
# private subnets behind NAT: the task needs outbound HTTPS to the price
# provider and listens on nothing, and a NAT Gateway would cost ~$43/month
# against ~$1/month for everything else here.

resource "aws_vpc" "stocks" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "stocks" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

# Two AZs so a zonal capacity problem does not stop the nightly run.
resource "aws_subnet" "public" {
  for_each = toset(["0", "1"])

  vpc_id                  = aws_vpc.stocks.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, tonumber(each.key))
  availability_zone       = data.aws_availability_zones.available.names[tonumber(each.key)]
  map_public_ip_on_launch = false # the task requests its own, per invocation

  tags = { Name = "stocks-public-${each.key}" }
}

resource "aws_internet_gateway" "stocks" {
  vpc_id = aws_vpc.stocks.id
  tags   = { Name = "stocks" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.stocks.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.stocks.id
  }

  tags = { Name = "stocks-public" }
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# Free, and keeps the database upload off the public path entirely.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.stocks.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = { Name = "stocks-s3" }
}

resource "aws_security_group" "task" {
  name        = "stocks-task"
  description = "Outbound HTTPS only; the task listens on nothing"
  vpc_id      = aws_vpc.stocks.id

  # Deliberately no ingress rule.
  egress {
    description = "HTTPS to the price provider, the symbol directory and AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "stocks-task" }
}
