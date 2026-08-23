resource "aws_ecs_cluster" "stocks" {
  name = "stocks"
}

resource "aws_ecs_task_definition" "universe" {
  for_each = local.universes

  family                   = "stocks-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    # Fargate x86. An arm64 image fails at task start with an exec format
    # error, so build with --platform linux/amd64.
    cpu_architecture = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "stocks"
    image     = "${aws_ecr_repository.stocks.repository_url}:${var.image_tag}"
    essential = true

    # The image entrypoint is `python /app/src/run.py`; this supplies its args.
    command = [
      "all",
      "--exchange", each.value.exchange,
      "--instrument-type", each.value.instrument_type,
      "--period", tostring(var.history_days),
    ]

    environment = [
      # STOCKS_DATA_ROOT is already baked into the image; repeated here so the
      # task definition is self-describing.
      { name = "STOCKS_DATA_ROOT", value = "/data" },
      { name = "S3_BUCKET", value = var.data_bucket },
      { name = "S3_REGION", value = var.region },
      # Both S3_BUCKET and S3_AUTO_UPLOAD are required before anything is
      # sent off the machine; without this the run publishes locally and logs
      # "Skipping S3 upload", which observability.tf alarms on.
      { name = "S3_AUTO_UPLOAD", value = "true" },
      { name = "TZ", value = "UTC" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.universe[each.key].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}
