# Creates the state bucket and lock table the main stack's backend needs.
# Chicken and egg: this config keeps its own state locally, and there is
# nothing here worth remote state.
#
#   cd infra/bootstrap
#   terraform init && terraform apply -var state_bucket=your-terraform-state-bucket

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "stocks"
      ManagedBy = "terraform-bootstrap"
    }
  }
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "state_bucket" {
  description = "Globally unique name for the Terraform state bucket."
  type        = string
}

variable "lock_table" {
  type    = string
  default = "terraform-locks"
}

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket

  # State is not reproducible: losing it means importing every resource by
  # hand. Refuse to destroy it by accident.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "locks" {
  name         = var.lock_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

output "backend_hcl" {
  description = "Paste into infra/backend.hcl."
  value       = <<-EOT
    bucket         = "${aws_s3_bucket.state.id}"
    key            = "stocks/terraform.tfstate"
    region         = "${var.region}"
    dynamodb_table = "${aws_dynamodb_table.locks.name}"
    encrypt        = true
  EOT
}
