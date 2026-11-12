# Container registry. Tags are immutable: a tag always means the same image (the pipeline tags with the git SHA),
# so "what is running" is answerable and a rollback is just an older tag.
resource "aws_ecr_repository" "app" {
  name                 = "pr-review-agent"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Delete untagged images after 7 days"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 7 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 15 newest images"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 15 }
        action       = { type = "expire" }
      },
    ]
  })
}

# The three secrets that come from outside AWS. Terraform creates the empty containers only; the VALUES are put
# with the CLI (infra/scripts/put-secrets.ps1), so they never appear in git or in Terraform state.
locals {
  external_secrets = toset(["github-webhook-secret", "github-private-key-base64", "anthropic-api-key"])
}

resource "aws_secretsmanager_secret" "external" {
  for_each = local.external_secrets

  name                    = "${var.project}/${each.key}"
  description             = "Set by hand with the CLI. Used by the ECS tasks."
  recovery_window_in_days = 7
}
