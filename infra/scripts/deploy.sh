#!/usr/bin/env bash
# Roll the running ECS services to an image that is already in ECR.
#   infra/scripts/deploy.sh <image-tag>
# Used by .github/workflows/deploy.yml and runnable by hand with AWS credentials in the environment.
#
# Order matters: (1) register new task definition revisions pointing at the new image, (2) run the database
# migration as a one-off task and require exit code 0, (3) only then move web and worker to the new revisions.
# A failed migration therefore never reaches the services. A bad new image is rolled back by ECS itself
# (deployment circuit breaker); this script also checks that the new revision really is the one running.
set -euo pipefail

TAG="${1:?usage: deploy.sh <image-tag>}"
REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-pr-review}"
CLUSTER="$PROJECT"
REPO="${ECR_REPOSITORY:-pr-review-agent}"
export AWS_DEFAULT_REGION="$REGION"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:${TAG}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Fail early, with a clear message, if the tag was never pushed.
aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$TAG" >/dev/null \
  || { echo "image tag $TAG is not in ECR repository $REPO" >&2; exit 1; }

register() {
  local family="$1"
  aws ecs describe-task-definition --task-definition "$family" --query taskDefinition --output json \
    | jq --arg img "$IMAGE" '
        del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy)
        | .containerDefinitions[0].image = $img' > "$TMP/$family.json"
  aws ecs register-task-definition --cli-input-json "file://$TMP/$family.json" \
    --query taskDefinition.taskDefinitionArn --output text
}

echo "==> registering task definitions for $IMAGE"
WEB_TD="$(register "$PROJECT-web")"
WORKER_TD="$(register "$PROJECT-worker")"
MIGRATE_TD="$(register "$PROJECT-migrate")"

echo "==> running migrations ($MIGRATE_TD)"
NETCFG="$(aws ecs describe-services --cluster "$CLUSTER" --services "$PROJECT-web" \
  --query 'services[0].networkConfiguration' --output json)"
TASK_ARN="$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$MIGRATE_TD" \
  --network-configuration "$NETCFG" --query 'tasks[0].taskArn' --output text)"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"
EXIT_CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)"
if [ "$EXIT_CODE" != "0" ]; then
  echo "migration failed (exit code $EXIT_CODE); services were NOT updated. Logs: /ecs/$PROJECT/migrate" >&2
  exit 1
fi

echo "==> updating services"
aws ecs update-service --cluster "$CLUSTER" --service "$PROJECT-web" --task-definition "$WEB_TD" >/dev/null
aws ecs update-service --cluster "$CLUSTER" --service "$PROJECT-worker" --task-definition "$WORKER_TD" >/dev/null
aws ecs wait services-stable --cluster "$CLUSTER" --services "$PROJECT-web" "$PROJECT-worker"

# If the circuit breaker rolled a service back, `wait` can still succeed: check what is really primary.
for pair in "web:$WEB_TD" "worker:$WORKER_TD"; do
  svc="${pair%%:*}"; want="${pair#*:}"
  got="$(aws ecs describe-services --cluster "$CLUSTER" --services "$PROJECT-$svc" \
    --query "services[0].deployments[?status=='PRIMARY'].taskDefinition | [0]" --output text)"
  if [ "$got" != "$want" ]; then
    echo "$svc is running $got, not $want: the deployment was rolled back" >&2
    exit 1
  fi
done
echo "==> deployed $TAG"
