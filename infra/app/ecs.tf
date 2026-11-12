locals {
  image = "${local.foundation.ecr_repository_url}:${var.image_tag}"

  # Plain settings. Anything not listed falls back to the defaults in app/config.py.
  environment = concat(
    [
      { name = "GITHUB_APP_ID", value = var.github_app_id },
      { name = "MODEL_REVIEW", value = var.model_review },
    ],
    [for k, v in var.app_settings : { name = k, value = v }],
  )

  # Secrets are fetched by ECS (not by the app) from Secrets Manager and injected as environment variables.
  secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "GITHUB_WEBHOOK_SECRET", valueFrom = local.foundation.secret_arns["github-webhook-secret"] },
    { name = "GITHUB_PRIVATE_KEY_BASE64", valueFrom = local.foundation.secret_arns["github-private-key-base64"] },
    { name = "ANTHROPIC_API_KEY", valueFrom = local.foundation.secret_arns["anthropic-api-key"] },
  ]

  secret_arns = concat([aws_secretsmanager_secret.database_url.arn], values(local.foundation.secret_arns))
}

resource "aws_ecs_cluster" "main" {
  name = var.project

  setting {
    name  = "containerInsights"
    value = "enabled" # gives the RunningTaskCount metric the alarms use
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each = toset(["web", "worker", "migrate"])

  name              = "/ecs/${var.project}/${each.key}"
  retention_in_days = 30
}

# ---------------------------------------------------------------------------------------------------------------
# IAM. Two roles with different jobs:
#   execution role = what ECS itself needs to START the task (pull the image, read the named secrets, write logs)
#   task role      = what the running APP may call in AWS. The app calls no AWS API, so it has no permissions.
# ---------------------------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.project}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "ReadOnlyTheNamedSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.secret_arns
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-app-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${var.project}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

# ---------------------------------------------------------------------------------------------------------------
# Task definitions: one image, three jobs (the same split as the old railway.json / railway.worker.json).
# ---------------------------------------------------------------------------------------------------------------
locals {
  def = {
    web = {
      cpu     = 256
      memory  = 512
      command = null
      port    = 8000
      stop    = 30
    }
    worker = {
      cpu     = 512
      memory  = 1024
      command = ["python", "-m", "app.jobs.worker"]
      port    = null
      stop    = 60 # longer than SHUTDOWN_GRACE_SECONDS (30); Fargate allows up to 120
    }
    migrate = {
      cpu     = 256
      memory  = 512
      command = ["alembic", "upgrade", "head"]
      port    = null
      stop    = 30
    }
  }
}

locals {
  port_settings = {
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  }
}

resource "aws_ecs_task_definition" "app" {
  for_each = local.def

  family                   = "${var.project}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    merge(
      {
        name        = each.key
        image       = local.image
        essential   = true
        stopTimeout = each.value.stop
        environment = local.environment
        secrets     = local.secrets
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.app[each.key].name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = each.key
          }
        }
      },
      # Optional keys: only the worker/migrate tasks override the command; only web has a port and health check.
      { for k, v in { command = each.value.command } : k => v if each.value.command != null },
      { for k, v in local.port_settings : k => v if each.value.port != null },
    )
  ])
}

# ---------------------------------------------------------------------------------------------------------------
# Services. The pipeline (infra/scripts/deploy.sh) moves them to new task definition revisions, so Terraform
# ignores `task_definition` here instead of fighting it. A bad deploy rolls back by itself (circuit breaker).
# ---------------------------------------------------------------------------------------------------------------
resource "aws_ecs_service" "web" {
  name            = "${var.project}-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app["web"].arn
  desired_count   = var.web_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.web.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http_forward, aws_lb_listener.http_redirect, aws_lb_listener.https]

  lifecycle {
    ignore_changes = [task_definition]
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${var.project}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app["worker"].arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}
