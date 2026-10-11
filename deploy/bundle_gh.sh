#!/usr/bin/env bash
# Download GitHub CLI for Linux ARM64 into the AgentCore deployment package (bin/gh).
set -euo pipefail

DEST="${1:?usage: bundle_gh.sh <pkg-dir>}"
GH_VERSION="${GH_VERSION:-2.63.2}"
ARCHIVE="gh_${GH_VERSION}_linux_arm64.tar.gz"
URL="https://github.com/cli/cli/releases/download/v${GH_VERSION}/${ARCHIVE}"

mkdir -p "$DEST/bin"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL "$URL" -o "$tmp/${ARCHIVE}"
tar -xzf "$tmp/${ARCHIVE}" -C "$tmp"
install -m 0755 "$tmp/gh_${GH_VERSION}_linux_arm64/bin/gh" "$DEST/bin/gh"

file "$DEST/bin/gh"
case "$(uname -s)-$(uname -m)" in
  Linux-aarch64|Linux-arm64)
    "$DEST/bin/gh" version
    ;;
  *)
    echo "bundled gh (linux arm64); version check skipped on $(uname -s)-$(uname -m)"
    ;;
esac
