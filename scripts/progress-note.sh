#!/usr/bin/env bash
set -euo pipefail
msg="${*:-no message}"
ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "[$ts] $msg" >> runtime/progress.log
echo "[$ts] $msg"
