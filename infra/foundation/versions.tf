terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial backend: the bucket name (it holds the account id) comes from backend.hcl, which is gitignored.
  #   terraform init -backend-config=backend.hcl
  # The very first apply runs with local state (the bucket does not exist yet); see infra/README.md.
  backend "s3" {
    key          = "foundation/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      Stack     = "foundation"
      ManagedBy = "terraform"
    }
  }
}
