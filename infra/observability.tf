# Three distinct failure modes, each needing a different detector:
#   1. the task ran and exited non-zero        -> EventBridge rule on the task
#   2. the task exited 0 but skipped upload    -> log metric filter + alarm
#   3. the task never ran at all               -> heartbeat alarm
# The third is the one silence hides, so its alarm treats missing data as
# breaching rather than waiting for a datapoint that will never arrive.
#
# A fourth lives in notifications.tf, because it belongs to the machinery it
# watches: the success notifier erroring, which also presents as silence.

resource "aws_cloudwatch_log_group" "universe" {
  for_each = local.universes

  name              = "/ecs/stocks/${each.key}"
  retention_in_days = var.log_retention_days
}

# ------------------------------------------------------------------- alerts

resource "aws_sns_topic" "alerts" {
  name = "stocks-alerts"
}

# AWS emails a confirmation link on first apply. Until it is clicked, nothing
# is delivered and the subscription shows as "PendingConfirmation".
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

data "aws_iam_policy_document" "alerts" {
  statement {
    sid       = "AllowCloudWatchAndEventBridge"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}

# ------------------------------------------- 1. the task exited non-zero

resource "aws_cloudwatch_event_rule" "task_failed" {
  name        = "stocks-task-failed"
  description = "A stocks task stopped with a non-zero exit code"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.stocks.arn]
      lastStatus = ["STOPPED"]
      containers = {
        exitCode = [{ "anything-but" = 0 }]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "task_failed" {
  rule      = aws_cloudwatch_event_rule.task_failed.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      group  = "$.detail.group"
      status = "$.detail.stoppedReason"
      task   = "$.detail.taskArn"
    }
    # Exit codes are stable: 1 error, 2 stale price data, 3 fetch too
    # incomplete to publish, 137 killed by the OOM killer.
    input_template = "\"stocks task failed: <group> (<status>) task=<task>\""
  }
}

# A task that never started has no container and therefore no exit code, so
# the rule above cannot see it. Image pull failures and ENI allocation
# failures land here.
resource "aws_cloudwatch_event_rule" "task_never_started" {
  name        = "stocks-task-never-started"
  description = "A stocks task stopped before its container ran"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn    = [aws_ecs_cluster.stocks.arn]
      lastStatus    = ["STOPPED"]
      stoppedReason = [{ "anything-but" = ["Essential container in task exited"] }]
    }
  })
}

resource "aws_cloudwatch_event_target" "task_never_started" {
  rule      = aws_cloudwatch_event_rule.task_never_started.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      group  = "$.detail.group"
      reason = "$.detail.stoppedReason"
    }
    input_template = "\"stocks task never started: <group> (<reason>)\""
  }
}

# ------------------------------- 2. exited 0 but published nothing to S3

# The upload is conditional on S3_BUCKET and S3_AUTO_UPLOAD. If either goes
# missing from the task definition the run succeeds and publishes nothing,
# which looks perfectly healthy from the outside. This is the detector for it.
resource "aws_cloudwatch_log_metric_filter" "upload_skipped" {
  for_each = local.universes

  name           = "stocks-${each.key}-upload-skipped"
  log_group_name = aws_cloudwatch_log_group.universe[each.key].name
  pattern        = "\"Skipping S3 upload\""

  metric_transformation {
    name          = "UploadSkipped"
    namespace     = "Stocks/${upper(each.key)}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "upload_skipped" {
  for_each = local.universes

  alarm_name        = "stocks-${each.key}-upload-skipped"
  alarm_description = "A ${upper(each.key)} run finished without publishing to S3. Check S3_BUCKET and S3_AUTO_UPLOAD on the task definition."

  namespace           = "Stocks/${upper(each.key)}"
  metric_name         = "UploadSkipped"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ------------------------------------------ 3. the run did not happen

resource "aws_cloudwatch_log_metric_filter" "upload_succeeded" {
  for_each = local.universes

  name           = "stocks-${each.key}-upload-succeeded"
  log_group_name = aws_cloudwatch_log_group.universe[each.key].name
  pattern        = "\"Uploaded to s3://\""

  metric_transformation {
    name          = "UploadSucceeded"
    namespace     = "Stocks/${upper(each.key)}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "no_upload" {
  for_each = local.universes

  alarm_name        = "stocks-${each.key}-no-upload-in-24h"
  alarm_description = "No ${upper(each.key)} database reached S3 in 24 hours. The schedule may not have fired, or the task is stuck provisioning."

  namespace           = "Stocks/${upper(each.key)}"
  metric_name         = "UploadSucceeded"
  statistic           = "Sum"
  period              = 86400 # the maximum a CloudWatch alarm allows
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # The point. A total absence of logs is exactly the condition being
  # detected; the default would leave this alarm INSUFFICIENT_DATA and silent.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
