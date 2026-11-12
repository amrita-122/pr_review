variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "pr-review"
}

variable "github_repository" {
  description = "owner/name of the GitHub repo allowed to deploy through OIDC."
  type        = string
}

variable "domain_name" {
  description = "Apex domain registered in Route53, e.g. example.com."
  type        = string
}

variable "app_hostname" {
  description = "Label under the domain where the webhook lives (review -> review.example.com)."
  type        = string
  default     = "review"
}

variable "enable_certificate" {
  description = "Create and validate the ACM certificate. Turn on only AFTER the domain's nameservers point at this zone, or validation waits forever."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Email subscribed to the alerts topic. Empty = no subscription (subscribe by hand with the CLI)."
  type        = string
  default     = ""
}
