output "ecr_repository_url" {
  description = "Push the image here. Build with --platform linux/amd64."
  value       = aws_ecr_repository.stocks.repository_url
}

output "ecr_push_commands" {
  description = "Copy-paste image push, run from the repository root."
  value = join("\n", [
    "aws ecr get-login-password --region ${var.region} | docker login --username AWS --password-stdin ${split("/", aws_ecr_repository.stocks.repository_url)[0]}",
    "docker build --platform linux/amd64 -t ${aws_ecr_repository.stocks.repository_url}:${var.image_tag} .",
    "docker push ${aws_ecr_repository.stocks.repository_url}:${var.image_tag}",
  ])
}

output "run_task_manually" {
  description = "Phase 2 of the rollout: run each task by hand before arming the schedules."
  value = {
    for k, v in local.universes :
    k => join(" ", [
      "aws ecs run-task --cluster ${aws_ecs_cluster.stocks.name}",
      "--task-definition ${aws_ecs_task_definition.universe[k].family}",
      "--launch-type FARGATE --region ${var.region}",
      "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", [for s in aws_subnet.public : s.id])}],securityGroups=[${aws_security_group.task.id}],assignPublicIp=ENABLED}'",
    ])
  }
}

output "log_groups" {
  description = "Where each universe's run output lands."
  value       = { for k, g in aws_cloudwatch_log_group.universe : k => g.name }
}

output "alert_topic_arn" {
  description = "Confirm the email subscription AWS sends, or no alert is delivered."
  value       = aws_sns_topic.alerts.arn
}

output "schedules" {
  description = "When each universe runs, in Melbourne time."
  value = {
    for k, s in aws_scheduler_schedule.universe :
    k => "${s.schedule_expression} ${s.schedule_expression_timezone} (${s.state})"
  }
}
