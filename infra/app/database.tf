# Alphanumeric only, so the password can sit inside a URL without escaping.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = var.project
  subnet_ids = aws_subnet.private[*].id
}

# TLS required for every connection (rds.force_ssl). The app also asks for it with ?sslmode=require.
resource "aws_db_parameter_group" "main" {
  name   = "${var.project}-postgres16"
  family = "postgres16"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }
}

resource "aws_db_instance" "main" {
  identifier = "${var.project}-db"

  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 50 # storage grows by itself up to this, then stops (a cost ceiling)
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "pr_review"
  username = "pr_review"
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  iam_database_authentication_enabled = true
  auto_minor_version_upgrade          = true
  backup_retention_period             = 7
  copy_tags_to_snapshot               = true
  deletion_protection                 = var.db_deletion_protection
  enabled_cloudwatch_logs_exports     = ["postgresql", "upgrade"]
  apply_immediately                   = true

  # `terraform destroy` keeps a snapshot, so tearing the demo down is not data loss. The name is fixed at
  # creation (a changing name would show a perpetual diff), hence ignore_changes.
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.project}-final-${formatdate("YYYYMMDDhhmmss", timestamp())}"

  lifecycle {
    ignore_changes = [final_snapshot_identifier, engine_version]
  }
}

# The one secret that is built here: the full URL the app reads as DATABASE_URL. It exists only while this stack
# does (recovery window 0 = deleted immediately on destroy, so a rebuild can reuse the name).
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.project}/database-url"
  description             = "DATABASE_URL for the ECS tasks (built from the generated RDS password)"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+psycopg://%s:%s@%s:%d/%s?sslmode=require",
    aws_db_instance.main.username,
    random_password.db.result,
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    aws_db_instance.main.db_name,
  )
}
