#!/usr/bin/env bash
# Install the private container's bwrap policy without weakening host defaults.
# Usage: sudo bash deploy/ovh/install-codex-sandbox.sh
set -euo pipefail
IFS=$'\n\t'
[[ "$(id -u)" == 0 ]] || { printf 'Run as root.\n' >&2; exit 2; }
policy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v apparmor_parser > /dev/null
[[ -r "$policy_dir/plugboard-codex.apparmor" ]]
[[ -r "$policy_dir/codex-seccomp.json" ]]
python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));assert d["defaultAction"]=="SCMP_ACT_ERRNO";assert all(r["action"]!="SCMP_ACT_ALLOW" or r["names"] for r in d["syscalls"])' "$policy_dir/codex-seccomp.json"
# Parse before installing/reloading; the profile applies to named containers only.
apparmor_parser -Q "$policy_dir/plugboard-codex.apparmor"
install -o root -g root -m 0644 "$policy_dir/plugboard-codex.apparmor" /etc/apparmor.d/plugboard-codex
apparmor_parser -r /etc/apparmor.d/plugboard-codex
printf 'Named AppArmor profile loaded; host userns sysctls are unchanged.\n'
