#!/usr/bin/env bash
# Build the AgentCore code zip: Linux ARM64 wheels + agent code, uploaded to S3.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/deploy/build"
BUCKET="${BUCKET:-cost-agent-artifacts-270149200236}"
KEY="${KEY:-cost-agent/agent-$(date +%Y%m%d%H%M%S).zip}"

rm -rf "$BUILD" && mkdir -p "$BUILD/pkg"
"$ROOT/deploy/bundle_gh.sh" "$BUILD/pkg"
uv pip install -q --target "$BUILD/pkg" --python-platform aarch64-manylinux2014 \
  --python-version 3.12 --only-binary=:all: -r "$ROOT/agent/requirements.txt"
cp "$ROOT/agent/agent.py" "$ROOT/agent/artifacts.py" "$ROOT/agent/github_fix.py" \
  "$ROOT/agent/memory.py" "$ROOT/agent/workspace_edit.py" "$BUILD/pkg/"
# Strands AgentSkills loads SKILL.md packages from this directory at runtime.
if [[ -d "$ROOT/agent/skills" ]]; then
  cp -R "$ROOT/agent/skills" "$BUILD/pkg/skills"
fi
(cd "$BUILD/pkg" && zip -qr "$BUILD/agent.zip" . -x '*__pycache__*')
aws s3 cp "$BUILD/agent.zip" "s3://$BUCKET/$KEY" --quiet
echo "s3://$BUCKET/$KEY ($(du -h "$BUILD/agent.zip" | cut -f1))"
echo "$KEY" > "$BUILD/last_key"
