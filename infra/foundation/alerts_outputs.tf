# Alerts topic lives here (not in the app stack) so an email subscription, once confirmed, survives destroys.
# Not encrypted with the AWS-managed SNS key on purpose: its key policy does not let CloudWatch/Budgets publish, so
# alarms would fail silently. The messages only name alarms; a customer-managed key would be the next step.
#checkov:skip=CKV_AWS_26:AWS-managed key blocks CloudWatch publishing; alert text holds no secrets
resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

data "aws_iam_policy_document" "alerts" {
  statement {
    sid       = "AllowCloudWatchAndBudgets"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "secret_arns" {
  description = "name => arn of the externally supplied secrets."
  value       = { for k, s in aws_secretsmanager_secret.external : k => s.arn }
}

output "hosted_zone_id" {
  value = aws_route53_zone.main.zone_id
}

output "name_servers" {
  description = "Set these as the domain's nameservers (Route53 > Registered domains) once the transfer is complete."
  value       = aws_route53_zone.main.name_servers
}

output "app_fqdn" {
  value = local.app_fqdn
}

output "certificate_arn" {
  value = var.enable_certificate ? aws_acm_certificate_validation.app[0].certificate_arn : null
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "github_deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}
