# Success notification: an email once a run has actually published.
#
# The alerts in observability.tf answer "did something break". This answers the
# opposite question -- "did tonight's database land" -- and it is deliberately
# driven by the S3 object rather than by the ECS task, because those are not
# the same event. A task can exit 0 having published nothing (that is failure
# mode 2 next door, when S3_BUCKET or S3_AUTO_UPLOAD goes missing from the task
# definition), so an "ECS task completed" email would arrive looking like
# success on exactly the night there is no new data. Watching the object means
# the email cannot be sent unless the database is really in the bucket.
#
# One email per database, not one per night: the two runs are 2h15m apart
# (07:15 and 09:30 Melbourne), so a combined message would have to hold the
# ASX result back until the US run finished, and would say nothing at all on a
# night when the second task never started.

# --------------------------------------------------------------- topic

# Separate from stocks-alerts on purpose. Routine success is ~2 messages a day
# and exceptional failure is ~0; putting them on one topic trains you to filter
# the topic, and the filter then swallows the alerts too. Split, the alert
# topic stays rare enough to be worth reading.
resource "aws_sns_topic" "notifications" {
  name = "stocks-notifications"
}

resource "aws_sns_topic_subscription" "notifications_email" {
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = local.notify_email
}

data "aws_iam_policy_document" "notifications" {
  statement {
    sid       = "AllowEventBridge"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.notifications.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "notifications" {
  arn    = aws_sns_topic.notifications.arn
  policy = data.aws_iam_policy_document.notifications.json
}

# ------------------------------------------------- bucket -> EventBridge

# S3 only emits to EventBridge once the bucket is told to. This is safe to add
# to a bucket the stack does not own -- it was verified empty before being
# written, which matters because aws_s3_bucket_notification manages the whole
# notification configuration and would silently drop any lambda or queue
# trigger already on the bucket rather than merging with it.
#
# It switches on events for every object in the bucket, which holds unrelated
# databases; the rule below is what narrows that back down to ours.
resource "aws_s3_bucket_notification" "data" {
  bucket      = data.aws_s3_bucket.data.id
  eventbridge = true
}

# ------------------------------------------------------------ the rule

resource "aws_cloudwatch_event_rule" "database_published" {
  name        = "stocks-database-published"
  description = "A stocks database was written to S3"

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [var.data_bucket] }
      # Exactly the two databases this stack publishes. The bucket also holds
      # nasdaq.db, trading.db and others that predate it; without this key
      # filter every write to any of them would send an email.
      object = { key = [for u in local.universes : u.database] }
    }
  })
}

resource "aws_cloudwatch_event_target" "database_published" {
  rule      = aws_cloudwatch_event_rule.database_published.name
  target_id = "sns"
  arn       = aws_sns_topic.notifications.arn

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
      size   = "$.detail.object.size"
      time   = "$.time"
    }
    # Size is in the message because it is the cheapest available check that
    # the run produced a real database: a screen that matched nothing still
    # publishes, and would arrive as a plausible-looking email a few hundred KB
    # short of the usual figure.
    input_template = "\"stocks: <key> published to s3://<bucket>/<key> at <time> (<size> bytes). The task completed and the database is live.\""
  }
}
