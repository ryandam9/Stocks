terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial configuration: the state bucket is named in backend.hcl, which
  # is gitignored because this repository is public. Locking is S3-native
  # (use_lockfile), so no DynamoDB table is involved.
  #
  #   terraform init -backend-config=backend.hcl
  #
  # Create it first with infra/bootstrap, which keeps state locally.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "stocks"
      ManagedBy = "terraform"
    }
  }
}

# Resolves the account ID at plan time so it is never written into the
# repository. Used only to scope IAM ARNs.
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}
