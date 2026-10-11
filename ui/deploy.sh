#!/usr/bin/env bash
# Copyright (c) 2026 DevRev Inc. All rights reserved.
# Deploy the AG-UI CloudFormation stack and wire it to the cost investigator runtime.
#
# Usage (from repo root):
#   ./ui/deploy.sh
#   ALLOWED_CIDR=203.0.113.10/32 ./ui/deploy.sh
#   STACK_NAME=cost-agent-ui ./ui/deploy.sh
#
# In GitHub Actions, set ALLOWED_CIDR via repository variable UI_ALLOWED_CIDR
# (defaults to 0.0.0.0/0 so the runner IP is not locked into CloudFront).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UI="$ROOT/ui"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
STACK_NAME="${STACK_NAME:-cost-agent-ui}"
RUNTIME_JSON="${RUNTIME_JSON:-$ROOT/deploy/build/runtime.json}"
RUNTIME_NAME="${RUNTIME_NAME:-cost_investigator}"
ALLOWED_CIDR="${ALLOWED_CIDR:-}"

resolve_runtime_arn() {
  if [[ -f "$RUNTIME_JSON" ]]; then
    python3 -c "import json; print(json.load(open('$RUNTIME_JSON'))['agentRuntimeArn'])"
    return
  fi
  echo "No $RUNTIME_JSON — resolving runtime '$RUNTIME_NAME' from AWS…" >&2
  local arn
  arn="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeArn | [0]" \
    --output text)"
  if [[ -z "$arn" || "$arn" == "None" ]]; then
    echo "Could not find AgentCore runtime named ${RUNTIME_NAME}." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$RUNTIME_JSON")"
  python3 -c "import json; json.dump({'agentRuntimeArn': '$arn'}, open('$RUNTIME_JSON', 'w'))"
  echo "$arn"
}

AGENT_RUNTIME_ARN="$(resolve_runtime_arn)"

if [[ -z "$ALLOWED_CIDR" ]]; then
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    ALLOWED_CIDR="0.0.0.0/0"
    echo "WARN: GITHUB_ACTIONS set and ALLOWED_CIDR unset — using 0.0.0.0/0." >&2
    echo "Set repository variable UI_ALLOWED_CIDR to lock CloudFront to your IP." >&2
  else
    MY_IP="$(curl -sS --max-time 5 https://checkip.amazonaws.com || true)"
    if [[ -n "$MY_IP" ]]; then
      ALLOWED_CIDR="${MY_IP}/32"
    else
      ALLOWED_CIDR="0.0.0.0/0"
      echo "WARN: could not detect public IP; using 0.0.0.0/0." >&2
    fi
  fi
fi

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ASSETS_BUCKET="cost-agent-ui-assets-${REGION}-${ACCOUNT}"
SECRET_NAME="${SECRET_NAME:-cost-agent-ui/api-secret}"

ensure_api_secret() {
  # Prefer explicit env (e.g. GitHub Actions secret COST_AGENT_API_SECRET).
  if [[ -n "${COST_AGENT_API_SECRET:-}" ]]; then
    echo "$COST_AGENT_API_SECRET"
    return
  fi
  if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
    aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" --region "$REGION" \
      --query SecretString --output text
    return
  fi
  local generated
  generated="$(openssl rand -hex 32)"
  aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --description "Shared secret for cost-agent-ui proxy (X-Cost-Agent-Secret)" \
    --secret-string "$generated" \
    --region "$REGION" >/dev/null
  echo "Created Secrets Manager secret ${SECRET_NAME}" >&2
  echo "$generated"
}

API_SECRET="$(ensure_api_secret)"
if [[ -z "$API_SECRET" || "$API_SECRET" == "None" ]]; then
  echo "Failed to resolve COST_AGENT_API_SECRET" >&2
  exit 1
fi

echo "Region:          $REGION"
echo "Stack:           $STACK_NAME"
echo "Runtime ARN:     $AGENT_RUNTIME_ARN"
echo "Allowed CIDR:    $ALLOWED_CIDR"
echo "Assets bucket:   $ASSETS_BUCKET"
echo "API secret:      Secrets Manager ${SECRET_NAME} (or COST_AGENT_API_SECRET env)"

echo "→ Building proxy Lambda zip"
(cd "$UI/deploy/proxy" && npm install --omit=dev --silent)
mkdir -p "$UI/deploy/.build"
rm -f "$UI/deploy/.build/proxy.zip"
(cd "$UI/deploy/proxy" && zip -rq ../.build/proxy.zip index.mjs package.json node_modules)

echo "→ Uploading proxy zip to s3://$ASSETS_BUCKET/proxy.zip"
aws s3 mb "s3://${ASSETS_BUCKET}" --region "$REGION" 2>/dev/null || true
aws s3 cp "$UI/deploy/.build/proxy.zip" "s3://${ASSETS_BUCKET}/proxy.zip" --region "$REGION"

PARAMS=(
  "ParameterKey=EnvironmentName,ParameterValue=${STACK_NAME}"
  "ParameterKey=AgentRuntimeArn,ParameterValue=${AGENT_RUNTIME_ARN}"
  "ParameterKey=AllowedIPv4CIDR,ParameterValue=${ALLOWED_CIDR}"
  "ParameterKey=ProxyAssetBucketParam,ParameterValue=${ASSETS_BUCKET}"
  "ParameterKey=ProxyAssetKeyParam,ParameterValue=proxy.zip"
)

deploy_stack() {
  if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "→ Updating stack $STACK_NAME"
    set +e
    UPDATE_OUT="$(aws cloudformation update-stack \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --template-body "file://${UI}/deploy/cloudformation.yaml" \
      --capabilities CAPABILITY_NAMED_IAM \
      --parameters "${PARAMS[@]}" 2>&1)"
    UPDATE_CODE=$?
    set -e
    if [[ $UPDATE_CODE -ne 0 ]]; then
      if echo "$UPDATE_OUT" | grep -qi "No updates are to be performed"; then
        echo "No CloudFormation template/parameter changes."
      else
        echo "$UPDATE_OUT" >&2
        exit "$UPDATE_CODE"
      fi
    else
      aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME" --region "$REGION"
    fi
  else
    echo "→ Creating stack $STACK_NAME (CloudFront can take ~5 minutes)"
    aws cloudformation create-stack \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --template-body "file://${UI}/deploy/cloudformation.yaml" \
      --capabilities CAPABILITY_NAMED_IAM \
      --parameters "${PARAMS[@]}"
    aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME" --region "$REGION"
  fi
}

deploy_stack

echo "→ Reading stack outputs"
eval "$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text \
  | python3 -c '
import sys
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line or "\t" not in line:
        continue
    k, v = line.split("\t", 1)
    print(f"export CF_{k}={v!r}")
')"

LAMBDA_NAME="${CF_ProxyLambdaName:?missing ProxyLambdaName}"
BUCKET="${CF_FrontendBucketName:?missing FrontendBucketName}"
DIST_ID="${CF_CloudFrontDistributionId:?missing CloudFrontDistributionId}"
FRONTEND_URL="${CF_FrontendUrl:?missing FrontendUrl}"
HTTP_URL="${CF_HttpApiUrl:?missing HttpApiUrl}"
WS_URL="${CF_WebSocketUrl:?missing WebSocketUrl}"

echo "→ Updating Lambda code + env (force code publish; CF ignores same S3 key)"
aws lambda update-function-code \
  --function-name "$LAMBDA_NAME" \
  --region "$REGION" \
  --s3-bucket "$ASSETS_BUCKET" \
  --s3-key proxy.zip \
  >/dev/null
aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$REGION"

# Use a file so the secret is not visible in process listings via argv.
ENV_FILE="$(mktemp)"
python3 - "$ENV_FILE" "$AGENT_RUNTIME_ARN" "$REGION" "$API_SECRET" <<'PY'
import json, sys
path, arn, region, secret = sys.argv[1:5]
json.dump(
    {
        "Variables": {
            "AGENT_RUNTIME_ARN": arn,
            "REGION": region,
            "AGENTCORE_ACTOR_ID": "cost-investigator",
            "COST_AGENT_API_SECRET": secret,
        }
    },
    open(path, "w"),
)
PY
aws lambda update-function-configuration \
  --function-name "$LAMBDA_NAME" \
  --region "$REGION" \
  --environment "file://${ENV_FILE}" \
  >/dev/null
rm -f "$ENV_FILE"
aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$REGION"

echo "→ Uploading frontend with API endpoints + secret injected"
TMP="$(mktemp)"
python3 - "$UI/frontend/index.html" "$TMP" "$HTTP_URL" "$WS_URL" "$API_SECRET" <<'PY'
import pathlib, sys
src, dst, http_url, ws_url, api_secret = sys.argv[1:6]
html = pathlib.Path(src).read_text()
inject = f"""<script>
window.__API_BASE = {http_url!r};
window.__WS_URL = {ws_url!r};
window.__API_SECRET = {api_secret!r};
</script>
"""
html = html.replace("<head>", "<head>\n" + inject, 1)
pathlib.Path(dst).write_text(html)
PY
aws s3 cp "$TMP" "s3://${BUCKET}/index.html" --region "$REGION" --content-type text/html
rm -f "$TMP"

echo "→ Invalidating CloudFront"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/index.html" "/" >/dev/null

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "## DevRev AWS Cost Investigator UI"
    echo "- Frontend: ${FRONTEND_URL}"
    echo "- HTTP API: ${HTTP_URL}"
    echo "- WebSocket: ${WS_URL}"
    echo "- Allowed CIDR: \`${ALLOWED_CIDR}\`"
    echo "- Auth: header \`X-Cost-Agent-Secret\` (secret in Secrets Manager \`${SECRET_NAME}\`)"
  } >>"$GITHUB_STEP_SUMMARY"
fi

cat <<EOF

Deployed.
  Frontend:  ${FRONTEND_URL}
  HTTP API:  ${HTTP_URL}
  WebSocket: ${WS_URL}
  CIDR:      ${ALLOWED_CIDR}
  Auth:      X-Cost-Agent-Secret (value in Secrets Manager: ${SECRET_NAME})

Workflow / curl example:
  aws secretsmanager get-secret-value --secret-id ${SECRET_NAME} --query SecretString --output text
  curl -X POST '${HTTP_URL}/invocations' \\
    -H 'Content-Type: application/json' \\
    -H "X-Cost-Agent-Secret: \$SECRET" \\
    -d '{"prompt":"hi","session_id":"session-workflow-xxxxxxxxxxxxxxxxx"}'

Note: the browser UI embeds the secret at deploy time (basic gate, not user auth).
EOF
