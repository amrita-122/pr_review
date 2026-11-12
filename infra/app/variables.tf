variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "pr-review"
}

variable "state_bucket" {
  description = "Name of the state bucket created by the foundation stack (its `state_bucket` output)."
  type        = string
}

variable "image_tag" {
  description = "Tag in the ECR repository to run. The pipeline moves services to new tags afterwards."
  type        = string
}

variable "github_app_id" {
  description = "The GitHub App's numeric id (not a secret)."
  type        = string
}

variable "model_review" {
  type    = string
  default = "claude-sonnet-5-5"
}

variable "enable_https" {
  description = "Serve HTTPS with the foundation certificate and redirect HTTP. Needs foundation `enable_certificate = true` first. While false the ALB answers plain HTTP: test with signed requests only, never point the real GitHub App at it."
  type        = bool
  default     = false
}

variable "enable_waf" {
  type    = bool
  default = true
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_deletion_protection" {
  description = "Keep false while the stack is destroyed between demos; turn on if it runs permanently."
  type        = bool
  default     = false
}

variable "web_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "app_settings" {
  description = "Extra non-secret environment variables for web and worker (names as in .env.example)."
  type        = map(string)
  default = {
    CONFIRMATION_ENABLED = "true"
    CONFIRM_SEVERITIES   = "critical"
    PRIVATE_CHANNEL      = "none" # ADR 0008/0011: stays none until the 10.21 hand check is done
    LANGSMITH_TRACING    = "false"
  }
}
