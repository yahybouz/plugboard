#!/usr/bin/env bash
# Verify local gateway authentication without any provider completion.
# Usage: ./deploy/ovh/verify.sh [http://127.0.0.1:8765]
set -euo pipefail
IFS=$'\n\t'
base="${1:-http://127.0.0.1:8765}"
check() {
  local path="$1" expected="$2" actual
  actual="$(curl --silent --show-error --max-time 15 --output /dev/null --write-out '%{http_code}' "${base}${path}")"
  if [[ "$actual" != "$expected" ]]; then
    printf 'FAIL %s: expected %s, got %s\n' "$path" "$expected" "$actual" >&2
    return 1
  fi
  printf 'PASS %s: %s\n' "$path" "$actual"
}
check /health 200
check /login 200
check /admin/mfa/status 401
check /v1/models 401
rejected_bearer="invalid-$(date +%s)-probe"  # deliberately wrong, must be refused
invalid="$(curl --silent --show-error --max-time 15 --output /dev/null --write-out '%{http_code}' -H "Authorization: Bearer ${rejected_bearer}" "${base}/v1/models")"
[[ "$invalid" == 401 ]] || { printf 'FAIL invalid bearer: %s\n' "$invalid" >&2; exit 1; }
printf 'PASS invalid bearer: 401\n'
printf 'No model completions were requested. Remote exposure and MFA enrollment require separate checks.\n'
