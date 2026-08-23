# ---------------------------------------------------------- task execution
# Used by the ECS agent, not by the pipeline: pulls the image, opens the log
# stream.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "stocks-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------- task role
# The only identity the pipeline itself uses. It writes two objects and does
# nothing else: the pipeline never reads from S3 (every artefact is rebuilt
# each run) and never lists the bucket, so GetObject and ListBucket are absent
# on purpose. This replaces the long-lived IAM user keys local runs use.

resource "aws_iam_role" "task" {
  name               = "stocks-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid     = "PublishDatabases"
    actions = ["s3:PutObject"]
    resources = [
      for u in local.universes : "arn:aws:s3:::${var.data_bucket}/${u.database}"
    ]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "publish-databases"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ----------------------------------------------------------- scheduler role
# Assumed by EventBridge Scheduler to launch the task.

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
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

resource "aws_iam_role" "scheduler" {
  name               = "stocks-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid     = "RunTheScheduledTask"
    actions = ["ecs:RunTask"]
    # Family wildcard, so a new task definition revision does not need the
    # policy reissued.
    resources = [
      for k, _ in local.universes :
      "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/stocks-${k}:*"
    ]
    condition {
      test     = "ArnLike"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.stocks.arn]
    }
  }

  statement {
    sid     = "PassTheTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.execution.arn,
      aws_iam_role.task.arn,
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "run-stocks-tasks"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
