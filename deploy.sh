#!/usr/bin/env bash
#
# fplBot deployment.
#
#   ./deploy.sh                      deploy the dev stack
#   ./deploy.sh --env prod           deploy production
#   ./deploy.sh --pipeline           deploy the CI/CD pipeline instead
#   ./deploy.sh --test-only          run the test suite and stop
#   ./deploy.sh --invoke             deploy, then invoke once and tail the logs
#   ./deploy.sh --delete --env dev   tear a stack down
#
# Configuration comes from a `.env` file next to this script. Run without one and
# it will offer to create it interactively.
#
# ---------------------------------------------------------------------------
# ON `set -euo pipefail`
# ---------------------------------------------------------------------------
#   -e            exit on any command failure
#   -u            treat unset variables as errors
#   -o pipefail   a pipeline fails if ANY stage fails, not just the last one
#
# The third is the one people miss, and it matters here: `sam build | tee log`
# without pipefail reports success whenever `tee` succeeds, which it always does.
# You would deploy a failed build and find out from CloudWatch.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

step()  { printf "\n%s==> %s%s\n" "$BOLD$BLUE" "$*" "$RESET"; }
info()  { printf "    %s\n" "$*"; }
ok()    { printf "    %s* %s%s\n" "$GREEN" "$*" "$RESET"; }
warn()  { printf "    %s! %s%s\n" "$YELLOW" "$*" "$RESET"; }
die()   { printf "\n%sERROR: %s%s\n\n" "$BOLD$RED" "$*" "$RESET" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Defaults, overridable from .env or the command line
# ---------------------------------------------------------------------------
ENVIRONMENT="dev"
# eu-west-2 (London) is the default for a reason: it is nearest the Fastly
# LHR/LCY points of presence that front the FPL API, which shaves real latency
# off every request. eu-west-1 (Dublin) is the other sensible choice.
AWS_REGION="${AWS_REGION:-eu-west-2}"
SEASON="2026-27"
STACK_PREFIX="fplbot"
DRY_RUN="false"
SCHEDULES_ENABLED="true"
LOG_LEVEL="INFO"
CONTACT_URL="https://github.com/MasonGallagher/fplBot"
ODDS_API_KEY_PARAMETER=""
ALARM_EMAIL="masongallagher90@gmail.com"
EMAIL_FROM=""
EMAIL_TO="masongallagher90@gmail.com"
GITHUB_CONNECTION_ARN=""
GITHUB_REPOSITORY="MasonGallagher/fplBot"
GITHUB_BRANCH="main"
NOTIFICATION_EMAIL=""

DO_PIPELINE="false"
DO_DELETE="false"
DO_TESTS="true"
DO_BUILD="true"
DO_INVOKE="false"
TEST_ONLY="false"
GUIDED="false"
USE_CONTAINER="true"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --env <dev|prod>     Target environment (default: dev)
  --region <region>    AWS region (default: eu-west-2)
  --pipeline           Deploy the CI/CD pipeline stack instead of the application
  --delete             Delete the stack instead of deploying it
  --test-only          Run tests and exit
  --skip-tests         Deploy without running tests (not recommended)
  --no-build           Reuse the previous .aws-sam build output
  --no-container       Build without Docker (faster, riskier - see the notes)
  --invoke             After deploying, invoke the function once and tail the logs
  --guided             Run `sam deploy --guided` for a first-time interactive setup
  --dry-run            Deploy with DRY_RUN=true (renders the report, sends nothing)
  --no-schedules       Deploy without arming the EventBridge schedules
  -h, --help           Show this help

EOF
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)          ENVIRONMENT="$2"; shift 2 ;;
    --region)       AWS_REGION="$2"; shift 2 ;;
    --pipeline)     DO_PIPELINE="true"; shift ;;
    --delete)       DO_DELETE="true"; shift ;;
    --test-only)    TEST_ONLY="true"; shift ;;
    --skip-tests)   DO_TESTS="false"; shift ;;
    --no-build)     DO_BUILD="false"; shift ;;
    --no-container) USE_CONTAINER="false"; shift ;;
    --invoke)       DO_INVOKE="true"; shift ;;
    --guided)       GUIDED="true"; shift ;;
    --dry-run)      DRY_RUN="true"; shift ;;
    --no-schedules) SCHEDULES_ENABLED="false"; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              die "Unknown option: $1 (try --help)" ;;
  esac
done

[[ "$ENVIRONMENT" =~ ^(dev|prod)$ ]] || die "--env must be 'dev' or 'prod', got '$ENVIRONMENT'"

STACK_NAME="${STACK_PREFIX}-${ENVIRONMENT}"
PIPELINE_STACK_NAME="${STACK_PREFIX}-pipeline"

# ---------------------------------------------------------------------------
# Configuration file
# ---------------------------------------------------------------------------
ENV_FILE="${SCRIPT_DIR}/.env"

create_env_file() {
  step "No .env found - let's create one"
  info "These values are stored locally in .env, which is gitignored."
  echo

  read -r -p "    Sender email (must be SES-verified): " EMAIL_FROM
  read -r -p "    Recipient email(s), comma-separated: " EMAIL_TO
  read -r -p "    Alarm email [${EMAIL_TO}]: " ALARM_EMAIL
  ALARM_EMAIL="${ALARM_EMAIL:-$EMAIL_TO}"
  read -r -p "    AWS region [${AWS_REGION}]: " region_input
  AWS_REGION="${region_input:-$AWS_REGION}"
  read -r -p "    Season [${SEASON}]: " season_input
  SEASON="${season_input:-$SEASON}"
  read -r -p "    The Odds API key (optional, blank to skip): " odds_key

  if [[ -n "$odds_key" ]]; then
    ODDS_API_KEY_PARAMETER="/fplbot/${ENVIRONMENT}/odds-api-key"
    step "Storing the Odds API key in SSM Parameter Store"
    # SecureString, not an environment variable: encrypted at rest, auditable in
    # CloudTrail, and rotatable without a redeploy. Parameter Store is free at
    # this scale, unlike Secrets Manager which bills per secret per month.
    aws ssm put-parameter \
      --name "$ODDS_API_KEY_PARAMETER" \
      --value "$odds_key" \
      --type SecureString \
      --overwrite \
      --region "$AWS_REGION" >/dev/null
    ok "Stored at $ODDS_API_KEY_PARAMETER"
  fi

  cat > "$ENV_FILE" <<EOF
# fplBot configuration. Generated by deploy.sh. Not committed to git.
AWS_REGION=${AWS_REGION}
SEASON=${SEASON}
EMAIL_FROM=${EMAIL_FROM}
EMAIL_TO=${EMAIL_TO}
ALARM_EMAIL=${ALARM_EMAIL}
CONTACT_URL=${CONTACT_URL}
ODDS_API_KEY_PARAMETER=${ODDS_API_KEY_PARAMETER}

# Pipeline settings - only needed for ./deploy.sh --pipeline
GITHUB_CONNECTION_ARN=
GITHUB_REPOSITORY=${GITHUB_REPOSITORY}
GITHUB_BRANCH=${GITHUB_BRANCH}

# Where CodePipeline sends build failures and the manual-approval notice.
# Defaults to ALARM_EMAIL - only set this if you want pipeline chatter going
# somewhere different from the "the bot is broken" alarms.
# NOTIFICATION_EMAIL=
EOF

  ok "Wrote $ENV_FILE"
}

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
elif [[ "$DO_DELETE" == "false" && "$TEST_ONLY" == "false" ]]; then
  create_env_file
fi

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
check_prerequisites() {
  step "Checking prerequisites"

  command -v aws >/dev/null 2>&1 || die \
    "The AWS CLI is not installed. See https://aws.amazon.com/cli/"
  ok "aws $(aws --version 2>&1 | cut -d' ' -f1 | cut -d/ -f2)"

  if [[ "$TEST_ONLY" == "false" ]]; then
    command -v sam >/dev/null 2>&1 || die \
      "The AWS SAM CLI is not installed.
       macOS:   brew install aws-sam-cli
       Windows: winget install Amazon.SAM-CLI
       Linux:   pip install aws-sam-cli"
    ok "sam $(sam --version 2>&1 | awk '{print $4}')"
  fi

  command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 || die \
    "Python is not installed."

  if [[ "$USE_CONTAINER" == "true" && "$DO_BUILD" == "true" && "$TEST_ONLY" == "false" ]]; then
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
      warn "Docker is unavailable, so falling back to a native build."
      warn "This matters: the Lambda runtime is Amazon Linux on aarch64. Binary"
      warn "wheels built against a different libc or architecture will import"
      warn "fine locally and fail inside Lambda with an unhelpful error."
      warn "numpy, orjson, selectolax and rapidfuzz all ship compiled code."
      USE_CONTAINER="false"
    else
      ok "docker available - building in a Lambda-compatible container"
    fi
  fi

  # Confirm credentials work before spending time on a build.
  local identity
  identity="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" || die \
    "AWS credentials are not configured or have expired. Run 'aws configure'."
  ok "Authenticated as ${identity}"
  info "Region: ${AWS_REGION}"
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
run_tests() {
  step "Running the test suite"

  local python_bin
  python_bin="$(command -v python3 || command -v python)"

  if ! "$python_bin" -c "import pytest" >/dev/null 2>&1; then
    info "Installing development dependencies..."
    "$python_bin" -m pip install --quiet -r layer/requirements.txt
    "$python_bin" -m pip install --quiet -e ".[dev]"
  fi

  # Dummy settings so imports that read the environment do not fail. The suite
  # never talks to AWS - these values are deliberately obvious placeholders.
  TABLE_NAME=test-table \
  BUCKET_NAME=test-bucket \
  EMAIL_FROM=test@example.com \
  EMAIL_TO=test@example.com \
  SEASON="$SEASON" \
  AWS_DEFAULT_REGION="$AWS_REGION" \
  PYTHONPATH=src \
    "$python_bin" -m pytest -m "not live" -q || die "Tests failed. Fix them before deploying."

  ok "All tests passed"
}

# ---------------------------------------------------------------------------
# SES
# ---------------------------------------------------------------------------
verify_ses_identities() {
  step "Checking SES identities"

  local addresses=()
  IFS=',' read -ra recipients <<< "$EMAIL_TO"
  addresses+=("$EMAIL_FROM")
  for recipient in "${recipients[@]}"; do
    addresses+=("$(echo "$recipient" | xargs)")
  done

  # A new SES account is in the *sandbox*: you may only send to verified
  # addresses, with a low daily cap. For a personal bot emailing its owner that
  # is entirely fine - verify the handful of addresses and never request
  # production access.
  local sandbox="unknown"
  sandbox="$(aws sesv2 get-account --region "$AWS_REGION" \
    --query 'ProductionAccessEnabled' --output text 2>/dev/null || echo "unknown")"

  if [[ "$sandbox" == "False" ]]; then
    info "SES is in sandbox mode - every recipient must be verified individually."
  fi

  local pending=()
  for address in "${addresses[@]}"; do
    [[ -z "$address" ]] && continue
    local status
    status="$(aws sesv2 get-email-identity --email-identity "$address" \
      --region "$AWS_REGION" --query 'VerifiedForSendingStatus' \
      --output text 2>/dev/null || echo "MISSING")"

    if [[ "$status" == "True" ]]; then
      ok "$address verified"
    else
      if [[ "$status" == "MISSING" ]]; then
        info "Creating SES identity for $address..."
        aws sesv2 create-email-identity --email-identity "$address" \
          --region "$AWS_REGION" >/dev/null 2>&1 || true
      fi
      pending+=("$address")
      warn "$address is NOT verified - AWS has emailed a confirmation link"
    fi
  done

  if [[ ${#pending[@]} -gt 0 ]]; then
    echo
    warn "Deployment will continue, but no email will send until these are verified:"
    for address in "${pending[@]}"; do
      warn "  - $address"
    done
    warn "Check the inbox for each and click the AWS confirmation link."
    echo
  fi
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
build_application() {
  step "Building"

  local build_args=(--template template.yaml --parallel)
  if [[ "$USE_CONTAINER" == "true" ]]; then
    # Builds each function and layer inside a container matching the Lambda
    # runtime, so compiled wheels are the right architecture and libc. Slower,
    # and worth it every time.
    build_args+=(--use-container --build-image public.ecr.aws/sam/build-python3.13:latest-arm64)
  fi

  sam build "${build_args[@]}" || die "sam build failed"

  # Guard the 250 MB unzipped budget across function plus layers. Catching this
  # here gives you a number and a hint; catching it at deploy time gives you a
  # rejected upload after a long wait.
  if [[ -d ".aws-sam/build/DependencyLayer" ]]; then
    local size_mb
    size_mb="$(du -sm .aws-sam/build/DependencyLayer | cut -f1)"
    info "Dependency layer: ${size_mb} MB unzipped (Lambda's limit is 250 MB total)"
    if [[ "$size_mb" -gt 220 ]]; then
      die "The layer is ${size_mb} MB, leaving under 30 MB of headroom.
       Check whether scipy or pandas has been pulled in transitively:
         pip install --dry-run -r layer/requirements.txt"
    fi
  fi

  ok "Build complete"
}

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
deploy_application() {
  step "Deploying ${STACK_NAME} to ${AWS_REGION}"

  if [[ "$GUIDED" == "true" ]]; then
    sam deploy --guided --stack-name "$STACK_NAME" --region "$AWS_REGION"
    return
  fi

  local parameters=(
    "Environment=${ENVIRONMENT}"
    "EmailFrom=${EMAIL_FROM}"
    "EmailTo=${EMAIL_TO}"
    "Season=${SEASON}"
    "ContactUrl=${CONTACT_URL}"
    "AlarmEmail=${ALARM_EMAIL}"
    "SchedulesEnabled=${SCHEDULES_ENABLED}"
    "DryRun=${DRY_RUN}"
    "LogLevel=${LOG_LEVEL}"
    "OddsApiKeyParameter=${ODDS_API_KEY_PARAMETER}"
  )

  sam deploy \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --resolve-s3 \
    --no-confirm-changeset \
    --no-fail-on-empty-changeset \
    --parameter-overrides "${parameters[@]}" \
    --tags Project=fplbot Environment="$ENVIRONMENT" ManagedBy=sam \
    || die "Deployment failed. Check the CloudFormation events:
       aws cloudformation describe-stack-events --stack-name ${STACK_NAME} --region ${AWS_REGION} --max-items 20"

  ok "Deployed"
}

deploy_pipeline() {
  step "Deploying the CI/CD pipeline stack"

  # Two different channels, and they are not the same thing:
  #
  #   ALARM_EMAIL        -> the application stack's SNS topic. "The bot is
  #                         broken": schema drift, a violated invariant, the
  #                         poll going silent. Fires at any hour, or never.
  #   NOTIFICATION_EMAIL -> the pipeline stack's SNS topic. "A deploy needs
  #                         you": build failed, or - the important one - a
  #                         manual approval is waiting. Fires only when you deploy.
  #
  # For a single-owner project they are almost always the same inbox, so this
  # defaults rather than asking twice. Set NOTIFICATION_EMAIL explicitly only to
  # split them, or to "-" to disable pipeline notifications while keeping alarms.
  #
  # The default matters: without it, an unset NOTIFICATION_EMAIL means the
  # manual-approval notice goes nowhere, and the approval gate is precisely the
  # thing that blocks a prod deploy. You would sit waiting for an email that was
  # never sent.
  local notification_email="${NOTIFICATION_EMAIL:-$ALARM_EMAIL}"
  [[ "$notification_email" == "-" ]] && notification_email=""

  if [[ -z "$notification_email" ]]; then
    warn "Pipeline notifications are disabled - you will not be emailed when a"
    warn "deploy is waiting on your manual approval. Watch the console instead."
  fi

  [[ -n "$GITHUB_CONNECTION_ARN" ]] || die \
    "GITHUB_CONNECTION_ARN is not set in .env.

       A CodeStar connection cannot be created non-interactively - the GitHub
       OAuth handshake needs a browser. Create one once:

         1. AWS Console -> Developer Tools -> Settings -> Connections
         2. Create connection -> GitHub -> authorise
         3. Copy the ARN into .env as GITHUB_CONNECTION_ARN"

  aws cloudformation deploy \
    --template-file pipeline/pipeline.yaml \
    --stack-name "$PIPELINE_STACK_NAME" \
    --region "$AWS_REGION" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
      "GitHubConnectionArn=${GITHUB_CONNECTION_ARN}" \
      "GitHubRepository=${GITHUB_REPOSITORY}" \
      "GitHubBranch=${GITHUB_BRANCH}" \
      "EmailFrom=${EMAIL_FROM}" \
      "EmailTo=${EMAIL_TO}" \
      "AlarmEmail=${ALARM_EMAIL}" \
      "Season=${SEASON}" \
      "NotificationEmail=${notification_email}" \
    --tags Project=fplbot Component=pipeline \
    || die "Pipeline deployment failed"

  ok "Pipeline deployed"

  local url
  url="$(aws cloudformation describe-stacks --stack-name "$PIPELINE_STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='PipelineUrl'].OutputValue" --output text)"
  info "Pipeline: $url"
}

delete_stack() {
  local target="$STACK_NAME"
  [[ "$DO_PIPELINE" == "true" ]] && target="$PIPELINE_STACK_NAME"

  step "Deleting ${target}"
  warn "This removes the Lambda functions, schedules and alarms."
  if [[ "$ENVIRONMENT" == "prod" ]]; then
    info "The prod DynamoDB table and S3 bucket have DeletionPolicy: Retain,"
    info "so your snapshot series and raw archive will survive. Delete them by"
    info "hand only if you are certain - the snapshot series cannot be rebuilt."
  fi

  read -r -p "    Type the stack name to confirm: " confirmation
  [[ "$confirmation" == "$target" ]] || die "Confirmation did not match. Nothing deleted."

  aws cloudformation delete-stack --stack-name "$target" --region "$AWS_REGION"
  info "Deletion requested. Waiting..."
  aws cloudformation wait stack-delete-complete --stack-name "$target" --region "$AWS_REGION" \
    && ok "Deleted" \
    || warn "Deletion did not complete cleanly - check the console."
}

# ---------------------------------------------------------------------------
# Post-deploy
# ---------------------------------------------------------------------------
show_outputs() {
  step "Stack outputs"
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
    --output table
}

invoke_once() {
  step "Invoking the poll function"

  local function_name="${STACK_PREFIX}-poll-${ENVIRONMENT}"
  local response_file
  response_file="$(mktemp)"

  aws lambda invoke \
    --function-name "$function_name" \
    --region "$AWS_REGION" \
    --cli-binary-format raw-in-base64-out \
    --payload '{}' \
    --log-type Tail \
    --query 'LogResult' --output text \
    "$response_file" | base64 --decode || warn "Invocation returned an error"

  echo
  info "Response:"
  cat "$response_file"; echo
  rm -f "$response_file"

  echo
  info "Follow the logs with:"
  info "  sam logs --stack-name ${STACK_NAME} --region ${AWS_REGION} --tail"
}

next_steps() {
  cat <<EOF

${BOLD}${GREEN}Deployment complete.${RESET}

${BOLD}What happens now${RESET}
  The poll function runs hourly at 7 minutes past, Europe/London. Every run
  snapshots FPL's data. When a deadline comes within 48h, 24h or 3h it also
  emails a board. The T-3h report is the one to act on - the earlier ones fire
  before most managers' press conferences.

${BOLD}Useful commands${RESET}
  Invoke now:      aws lambda invoke --function-name ${STACK_PREFIX}-poll-${ENVIRONMENT} --region ${AWS_REGION} /dev/stdout
  Force an email:  aws lambda invoke --function-name ${STACK_PREFIX}-poll-${ENVIRONMENT} --region ${AWS_REGION} \\
                     --cli-binary-format raw-in-base64-out --payload '{"force_tier":"48h"}' /dev/stdout
  Tail logs:       sam logs --stack-name ${STACK_NAME} --region ${AWS_REGION} --tail
  Local run:       sam local invoke PollFunction --event events/scheduled.json

${BOLD}Before the first real send${RESET}
  1. Confirm the SES verification emails (check spam).
  2. Deploy once with --dry-run and read the rendered report in CloudWatch Logs.
  3. Subscribe to the alarm SNS topic - AWS emails a confirmation link.

EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  printf "\n%sfplBot deployment%s  -  environment: %s, region: %s\n" \
    "$BOLD" "$RESET" "$ENVIRONMENT" "$AWS_REGION"

  if [[ "$TEST_ONLY" == "true" ]]; then
    run_tests
    exit 0
  fi

  check_prerequisites

  if [[ "$DO_DELETE" == "true" ]]; then
    delete_stack
    exit 0
  fi

  if [[ "$DO_PIPELINE" == "true" ]]; then
    deploy_pipeline
    exit 0
  fi

  [[ -n "$EMAIL_FROM" ]] || die "EMAIL_FROM is not set. Add it to .env."
  [[ -n "$EMAIL_TO" ]]   || die "EMAIL_TO is not set. Add it to .env."

  [[ "$DO_TESTS" == "true" ]] && run_tests
  verify_ses_identities
  [[ "$DO_BUILD" == "true" ]] && build_application
  deploy_application
  show_outputs
  [[ "$DO_INVOKE" == "true" ]] && invoke_once
  next_steps
}

main "$@"
