output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "webhook_url" {
  description = "Set this as the GitHub App's webhook URL (HTTPS only)."
  value       = var.enable_https ? "https://${local.hostname}/webhooks/github" : "http://${aws_lb.main.dns_name}/webhooks/github (HTTP, test only)"
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "services" {
  value = { web = aws_ecs_service.web.name, worker = aws_ecs_service.worker.name }
}

output "migrate_task_definition" {
  value = aws_ecs_task_definition.app["migrate"].family
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "worker_security_group_id" {
  value = aws_security_group.worker.id
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}
