# ADR 0011 — Deploy on AWS (ECS Fargate, RDS, Terraform)

Status: Proposed · Date: 2026-11-11

## Context
The app runs on Railway: a web service and a worker from one Docker image (`railway.json`,
`railway.worker.json`) plus a managed Postgres. The owner is **leaving Railway for good**; AWS replaces it, it does
not sit beside it. The goal is to deploy on AWS using good practice, as a portfolio piece. The stack rule in
CLAUDE.md says a stack change needs an ADR, and "no new services without an ADR".

Constraints:
- The app already follows twelve-factor rules (config from env vars, JSON logs, `/health`, graceful shutdown,
  migrations as a separate step), so the move should need **no application changes**.
- The eval numbers in the README were measured against the direct Anthropic API with `claude-sonnet-5-5`.
  Changing the model provider would invalidate them.
- The stack will be **spun up for demos and destroyed between them**, so rebuilding must be fast and repeatable.
- Anything the bot writes to a public PR is public (ADR 0008). Moving hosts must not change that.

## Options considered
**Compute**
1. **ECS on Fargate**, web and worker as two services from one image. Maps 1:1 to the two Railway services.
2. App Runner: cannot run the worker, which has no HTTP port.
3. Lambda + SQS: re-architects the queue that ADR 0005 chose deliberately (transactional with the webhook).
4. EKS: Kubernetes is excluded by CLAUDE.md and is overkill for two services.
5. EC2: more to patch and operate, with no benefit here.

**Database**
1. **RDS Postgres 16** (db.t4g.micro): closest to what the app already uses, cheap, simple to destroy.
2. Aurora Serverless v2: nicer scaling, higher floor cost, nothing the app needs.

**Model provider**
Stay on the Anthropic API. Bedrock would be more "AWS" but needs a new integration and a full eval re-run
(listed under Revisit).

**Infrastructure as code**
1. **Terraform** (chosen): widely used, readable, portable, with S3 remote state.
2. CDK (Python): same language as the app but generates CloudFormation and is less portable.

## Decision
Keep the application and the Postgres queue unchanged. Move only the hosting.

**Two Terraform stacks** so that destroy and rebuild is safe:

- `infra/foundation` (persistent, a few dollars a month at most): S3 state bucket with native locking, Route53
  hosted zone and DNS-validated ACM certificate, ECR repository (scan on push, lifecycle policy), Secrets Manager
  entries for the three external secrets (GitHub App private key, webhook secret, Anthropic key; values set with
  the CLI, never in git or Terraform state), GitHub Actions OIDC provider and deploy role, AWS Budget alert.
- `infra/app` (destroyable): VPC across two AZs (public and private subnets, one NAT gateway, S3 gateway
  endpoint), RDS Postgres, ECS cluster with `web` and `worker` services, ALB, WAF, CloudWatch alarms and dashboard,
  SNS email.

**Compute.** One ECR image. `web` uses the default `CMD`, container port 8000, ALB target group health check on
`/health`. `worker` overrides the command to `python -m app.jobs.worker` and has no port. Migrations run as a
one-off ECS task (`alembic upgrade head`) before the services are updated, like Railway's pre-deploy command. Both
services use the deployment circuit breaker (automatic rollback). Worker `stopTimeout` is set to cover
`SHUTDOWN_GRACE_SECONDS` (30 s); the Fargate maximum is 120 s. If a worker is killed anyway, the existing job lease
(ADR 0005) puts the job back in the queue.

**Network and security.**
- ECS tasks and RDS in private subnets. Inbound only ALB to web; RDS reachable only from the task security group.
- ALB serves HTTPS with the ACM certificate and redirects HTTP to HTTPS. A Route53 alias keeps the webhook URL
  identical across rebuilds, so the GitHub App setting is changed once.
- WAF on the ALB: AWS managed rule groups and a rate-based rule. The HMAC check and the 2 MB body cap in
  `app/main.py` stay the primary defence; WAF is defence in depth.
- Two IAM roles per task, each least privilege: the execution role pulls the image and reads the named secrets; the
  task role holds nothing the app does not need (the app calls no AWS API).
- RDS encrypted at rest, not publicly accessible, TLS required, automated backups, final snapshot on destroy.
- Secrets are injected by ECS from Secrets Manager. `DATABASE_URL` is assembled by Terraform from a generated
  password (alphanumeric only, so no URL escaping) with `?sslmode=require`. `Settings.sqlalchemy_url` and
  `checkpoint_conninfo` already pass query parameters through; this is checked in step 11.5, not assumed.

**Delivery.** GitHub Actions with OIDC, no stored AWS keys: existing CI, build, Trivy image scan, push to ECR,
migration task, service update, wait for stable. CI checks the Terraform (`fmt`, `validate`, `checkov`) without AWS
access. **`terraform plan` and `apply` are run by hand, not in CI**: a role that can apply this stack would need
near-admin rights (IAM, VPC, RDS), a larger risk than the convenience is worth for a one-owner project. The deploy
role used by the pipeline can only ship an image and update the two services.

**Observability.** structlog JSON goes to CloudWatch Logs with a retention period. Alarms: ALB 5xx, unhealthy
targets, running task count below 1 for either service, RDS CPU, free storage and connections, failed-job alerts
through the existing `ALERT_WEBHOOK_URL`. The budget alert covers cost.

**Railway is retired.** A GitHub App has one webhook URL, so cut-over is a switch, not a parallel run: Railway keeps
serving until the AWS deployment passes the step 11.10 checks, then the App's URL is changed once. After that the
Railway project is deleted, `railway.json` and `railway.worker.json` are removed from the repo, and the secrets that
lived on Railway (webhook secret, GitHub private key, Anthropic key) are rotated, because a retired host should not
hold live credentials. There is no fallback host; a failed AWS deploy is fixed forward or rolled back by the pipeline.
ADR 0001 (hosting choice) and ADR 0005 (worker as a second Railway service) are superseded for hosting only; their
other decisions stand.

**Visibility rule unchanged.** `PRIVATE_CHANNEL` stays `none` until step 10.21 is done by hand (ADR 0008). The
deployment is installed on sandbox or private repos only until then.

## Consequences
+ No application change; the 673 tests and eval results stay valid.
+ Everything is code: reviewable, repeatable, destroyable. Idle cost is a few dollars a month.
+ Shows VPC design, IAM least privilege, ECS, RDS, ALB/ACM/WAF, Secrets Manager, CloudWatch, Terraform state,
  OIDC-based CI/CD and cost control.
- Higher fixed cost than Railway was while running (NAT gateway and ALB are the largest items). Figures are
  estimates until measured in 11.12.
- No second host: while the stack is destroyed between demos, the webhook has nowhere to land. GitHub keeps
  failed deliveries, so Redeliver works after a rebuild, but reviews are not instant while it is down.
- A single NAT gateway is one AZ's egress point. Accepted for a demo; a production setup would use one per AZ.
- The generated database password lives in Terraform state. The state bucket is private and encrypted.
- Destroying the app stack deletes the database; a final snapshot is kept, but queue and run history are demo data.
- More to maintain: two Terraform stacks and a pipeline.
- The domain (about $12-15 a year) is the one non-refundable cost.

## Revisit when
- The app needs more than one worker or more throughput: autoscale on queue depth (emit an EMF log line).
- It runs permanently: Multi-AZ RDS, one NAT per AZ, ARM (Graviton) tasks.
- Bedrock is wanted as a provider: add a model integration and re-run every eval before claiming numbers.
- A second environment is needed: turn `infra/app` into a module with per-environment variables.
