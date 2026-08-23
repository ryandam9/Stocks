# The data bucket itself is not managed here -- it predates this stack and
# holds unrelated objects. Versioning and expiry are separate resources, so
# they can be applied to it without importing the bucket.

data "aws_s3_bucket" "data" {
  bucket = var.data_bucket
}

# Without this, a run that publishes a subtly wrong database destroys the last
# good copy. The pipeline can always rebuild, but rebuilding is not the same as
# recovering the exact artefact a consumer already read.
resource "aws_s3_bucket_versioning" "data" {
  bucket = data.aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Bounds what versioning costs. At ~8 MB a day for both databases this is a
# few cents a month.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket     = data.aws_s3_bucket.data.id
  depends_on = [aws_s3_bucket_versioning.data]

  rule {
    id     = "expire-old-database-versions"
    status = "Enabled"

    filter {
      prefix = ""
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
