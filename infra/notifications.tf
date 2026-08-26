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
#
# S3 event -> EventBridge rule -> lambda -> SES -> inbox.

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

# The whole event goes to the lambda -- no input_transformer, because the
# lambda needs $.time raw in order to convert it.
resource "aws_cloudwatch_event_target" "database_published" {
  rule      = aws_cloudwatch_event_rule.database_published.name
  target_id = "notify"
  arn       = aws_lambda_function.notify.arn
}

resource "aws_lambda_permission" "notify" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.database_published.arn
}

# --------------------------------------------------------------- sender

# SES rather than SNS, which is why there is no topic here any more. An SNS
# email subscription sends plain text only: the body arrives as a quoted
# string under the subject "AWS Notification Message", followed by an
# unsubscribe footer, and no amount of formatting in the lambda changes any of
# that. SES takes an HTML body and a subject of our choosing.
#
# The sending domain is verified with Easy DKIM: SES generates three keys and
# the CNAMEs below publish them, which is what lets a receiver check the
# signature. Sending from an unaligned address -- a gmail.com From on mail
# signed by amazonses.com -- fails DMARC alignment and lands in spam, so a
# domain you control is not a nicety here.
resource "aws_sesv2_email_identity" "sender" {
  email_identity = var.notify_domain

  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }
}

data "aws_route53_zone" "notify" {
  name         = var.notify_domain
  private_zone = false
}

# Three, always: SES rotates through them, and a domain with two of the three
# published verifies and then silently stops signing later.
resource "aws_route53_record" "notify_dkim" {
  count = 3

  zone_id = data.aws_route53_zone.notify.zone_id
  name    = "${aws_sesv2_email_identity.sender.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.notify_domain}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_sesv2_email_identity.sender.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}

# The recipient, verified as an identity in its own right. Only the SES
# sandbox requires this -- outside it any address can be written to -- but a
# new account is in the sandbox, and an unverified recipient there fails the
# send rather than queuing it. AWS emails a confirmation link on first apply,
# exactly like the SNS subscription this replaces; until it is clicked, the
# lambda errors and stocks-notify-failed alarms.
resource "aws_sesv2_email_identity" "recipient" {
  email_identity = local.notify_email
}

# --------------------------------------------------------- the notifier

# Why a lambda sits between an event and an email: the time, and the format.
#
# EventBridge's input transformer substitutes values and never converts them,
# so a direct target can only quote $.time, which is UTC -- "published at
# 2026-08-25T21:34:50Z" for a database that landed at 07:34 the next morning.
# Every other time in this stack is Melbourne local, and the correction is ten
# hours for half the year and eleven for the other half, with the date moving
# too. The function formats the timestamp in local.timezone, names the offset
# in force (AEST or AEDT), and renders the HTML body.
#
# It is a single file with no dependencies -- boto3 ships in the runtime -- so
# there is no layer and no build step beyond zipping it.
data "archive_file" "notify" {
  type        = "zip"
  source_file = "${path.module}/lambda/notify_published.py"
  output_path = "${path.module}/build/notify_published.zip"
}

data "aws_iam_policy_document" "notify_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "notify" {
  name               = "stocks-notify"
  assume_role_policy = data.aws_iam_policy_document.notify_assume.json
}

# Written out rather than attaching AWSLambdaBasicExecutionRole, which grants
# logs:CreateLogGroup across the account. This function writes to the one
# group terraform created for it -- and because it cannot create a log group,
# the retention below is the only setting the group will ever have.
data "aws_iam_policy_document" "notify" {
  statement {
    sid     = "SendFromTheVerifiedDomain"
    actions = ["ses:SendEmail"]
    # The identity being sent *from*, not the recipient. Scoped to this one
    # domain, so the role cannot send as anything else the account verifies
    # later.
    resources = [aws_sesv2_email_identity.sender.arn]
  }

  statement {
    sid       = "WriteItsOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.notify.arn}:*"]
  }
}

resource "aws_iam_role_policy" "notify" {
  name   = "send-notifications"
  role   = aws_iam_role.notify.id
  policy = data.aws_iam_policy_document.notify.json
}

resource "aws_cloudwatch_log_group" "notify" {
  name              = "/aws/lambda/stocks-notify"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "notify" {
  function_name = "stocks-notify"
  description   = "Emails the database-published notice, in ${local.timezone}"
  role          = aws_iam_role.notify.arn

  filename         = data.archive_file.notify.output_path
  source_code_hash = data.archive_file.notify.output_base64sha256
  handler          = "notify_published.handler"
  runtime          = "python3.12"

  # One SES call. The default 3s is generous already; 10s only covers a cold
  # start that has to resolve credentials on a bad day.
  timeout     = 10
  memory_size = 128

  environment {
    variables = {
      FROM_ADDRESS = local.notify_from
      TO_ADDRESS   = local.notify_email
      # The zone the schedules already run on, so the email and the cron are
      # never quoting two different clocks.
      TIMEZONE = local.timezone
    }
  }

  # Without this the function's first invocation creates the log group itself
  # -- except it has no permission to, so the race is worth removing anyway.
  depends_on = [aws_cloudwatch_log_group.notify]
}

# Putting a function in the path adds a failure mode a direct SNS target did
# not have: the database lands, the notifier throws, and the absence of an
# email looks exactly like the absence of a run. This is the detector for it,
# and it is also what reports an unverified SES identity -- the one way this
# path fails on a first apply.
resource "aws_cloudwatch_metric_alarm" "notify_failed" {
  alarm_name        = "stocks-notify-failed"
  alarm_description = "The success notifier errored: a database may have been published without an email. Check /aws/lambda/stocks-notify, and that the SES identities are verified."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions  = { FunctionName = aws_lambda_function.notify.function_name }

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # Errors are published only when there are some, and the function runs twice
  # a day: nearly every period is legitimately empty.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
