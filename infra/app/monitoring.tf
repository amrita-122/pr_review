locals {
  alert_topic = local.foundation.alerts_topic_arn
}

# ALB: server errors reaching the app, and "no healthy web task".
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.project}-alb-target-5xx"
  alarm_description   = "The web tasks returned 5xx responses"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic]
  ok_actions          = [local.alert_topic]
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name          = "${var.project}-unhealthy-web-tasks"
  alarm_description   = "A web task is failing its /health check"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.web.arn_suffix }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic]
  ok_actions          = [local.alert_topic]
}

# ECS: a service with fewer running tasks than it should have (the worker has no load balancer to notice).
resource "aws_cloudwatch_metric_alarm" "running_tasks" {
  for_each = { web = var.web_desired_count, worker = var.worker_desired_count }

  alarm_name          = "${var.project}-${each.key}-running-tasks"
  alarm_description   = "The ${each.key} service has fewer running tasks than desired"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = aws_ecs_cluster.main.name, ServiceName = "${var.project}-${each.key}" }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = each.value
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [local.alert_topic]
  ok_actions          = [local.alert_topic]
}

# RDS
resource "aws_cloudwatch_metric_alarm" "rds" {
  for_each = {
    cpu         = { metric = "CPUUtilization", stat = "Average", op = "GreaterThanThreshold", threshold = 80, desc = "CPU above 80%" }
    storage     = { metric = "FreeStorageSpace", stat = "Minimum", op = "LessThanThreshold", threshold = 2147483648, desc = "Less than 2 GiB free" }
    connections = { metric = "DatabaseConnections", stat = "Maximum", op = "GreaterThanThreshold", threshold = 60, desc = "More than 60 connections" }
  }

  alarm_name          = "${var.project}-rds-${each.key}"
  alarm_description   = "Database: ${each.value.desc}"
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = each.value.stat
  period              = 300
  evaluation_periods  = 3
  threshold           = each.value.threshold
  comparison_operator = each.value.op
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic]
  ok_actions          = [local.alert_topic]
}

# Application errors: the app logs JSON ({"level": "error", ...}); count them and alarm on a burst. Only the
# level is matched, never the message, so nothing sensitive is copied into a metric.
resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each = toset(["web", "worker"])

  name           = "${var.project}-${each.key}-errors"
  log_group_name = aws_cloudwatch_log_group.app[each.key].name
  pattern        = "{ $.level = \"error\" }"

  metric_transformation {
    # One metric per service (a dimension value must be a selector into the log line, not a constant, and
    # default_value cannot be combined with dimensions). Missing data is handled by the alarm.
    name      = "AppErrors-${each.key}"
    namespace = "PRReview"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "app_errors" {
  for_each = toset(["web", "worker"])

  alarm_name          = "${var.project}-${each.key}-error-logs"
  alarm_description   = "5 or more error log lines in 5 minutes from the ${each.key}"
  namespace           = "PRReview"
  metric_name         = "AppErrors-${each.key}"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic]
  ok_actions          = [local.alert_topic]

  depends_on = [aws_cloudwatch_log_metric_filter.errors]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.project

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Requests and 5xx (ALB)"
          region = var.region
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.main.arn_suffix],
            [".", "HTTPCode_Target_5XX_Count", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Running tasks"
          region = var.region
          stat   = "Minimum"
          period = 60
          metrics = [
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project}-web"],
            ["...", "${var.project}-worker"],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Task CPU and memory (%)"
          region = var.region
          stat   = "Average"
          period = 300
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project}-web"],
            [".", "MemoryUtilization", ".", ".", ".", "."],
            [".", "CPUUtilization", ".", ".", ".", "${var.project}-worker"],
            [".", "MemoryUtilization", ".", ".", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Database CPU and connections"
          region = var.region
          stat   = "Average"
          period = 300
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.main.identifier],
            [".", "DatabaseConnections", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 24, height = 6
        properties = {
          title  = "Application error logs"
          region = var.region
          stat   = "Sum"
          period = 300
          metrics = [
            ["PRReview", "AppErrors-web"],
            ["PRReview", "AppErrors-worker"],
          ]
        }
      },
    ]
  })
}
