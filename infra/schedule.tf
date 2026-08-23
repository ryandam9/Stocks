# EventBridge Scheduler rather than a CloudWatch Events rule: only Scheduler
# accepts an IANA timezone, and Melbourne's DST transitions would otherwise
# move the run an hour twice a year.

resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "stocks-scheduler-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_scheduler_schedule" "universe" {
  for_each = local.universes

  name        = "stocks-${each.key}"
  group_name  = "default"
  state       = var.schedule_enabled ? "ENABLED" : "DISABLED"
  description = "Daily ${upper(each.key)} screen at 20:00 ${local.timezone}"

  # Nothing downstream depends on the exact minute, so let AWS spread the
  # invocation. Set mode to "OFF" to pin it to 20:00 precisely.
  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15
  }

  schedule_expression          = each.value.cron
  schedule_expression_timezone = local.timezone

  target {
    arn      = aws_ecs_cluster.stocks.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.universe[each.key].arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = [for s in aws_subnet.public : s.id]
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }

    # Retries the RunTask API call, not a task that ran and exited non-zero.
    # Exit-code retries would need Step Functions; they are not worth building
    # because every run refetches a full year, so tomorrow's run fully repairs
    # a day that failed.
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }

    dead_letter_config {
      arn = aws_sqs_queue.scheduler_dlq.arn
    }
  }
}

data "aws_iam_policy_document" "scheduler_dlq" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduler_dlq.arn]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "scheduler_dlq" {
  queue_url = aws_sqs_queue.scheduler_dlq.id
  policy    = data.aws_iam_policy_document.scheduler_dlq.json
}
