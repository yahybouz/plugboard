#!/usr/bin/env bash
# Cold, encrypted backup of the single gateway. Run as root.
# Usage: backup.sh [destination-directory]
set -euo pipefail
IFS=$'\n\t'
umask 077
[[ "$EUID" -eq 0 ]] || exit 1
exec 9>/run/lock/plugboard-backup.lock
flock -n 9 || { printf 'Backup already running\n' >&2; exit 1; }
cd /opt/plugboard
compose=(docker compose -f deploy/ovh/compose.yml)
cid="$("${compose[@]}" ps -aq gateway)"
[[ -n "$cid" ]] || { printf 'Gateway container missing\n' >&2; exit 1; }
image="$(docker inspect --format '{{.Config.Image}}' "$cid")"
recipient="$(< /etc/plugboard/backup.recipient)"
[[ "$recipient" == age1* ]] || { printf 'Invalid backup recipient\n' >&2; exit 1; }
destination="${1:-/var/backups/plugboard}"
install -d -m 0700 "$destination"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$destination/plugboard-$stamp.tar.gz.age"
[[ ! -e "$archive" && ! -e "$archive.partial" ]] || exit 1
running="$(docker inspect --format '{{.State.Running}}' "$cid")"
cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "$running" == true ]]; then
    if ! docker start "$cid" >/dev/null; then
      printf 'ERROR: gateway restart failed; manual intervention required\n' >&2
      rc=1
    fi
  fi
  [[ ! -e "$archive.partial" ]] || rm -f -- "$archive.partial"
  exit "$rc"
}
trap cleanup EXIT
if [[ "$running" == true ]]; then docker stop --time 40 "$cid" >/dev/null; fi
# Root is needed for CLI-owned private files; all source volumes stay read-only.
docker run --rm --user 0:0 --network none \
  --volumes-from "$cid:ro" \
  --mount type=bind,src=/etc/plugboard/runtime.env,dst=/backup-config/runtime.env,readonly \
  --entrypoint tar "$image" -C / -czf - app/data home/app backup-config/runtime.env \
  | age --recipient "$recipient" > "$archive.partial"
[[ -s "$archive.partial" ]]
mv -- "$archive.partial" "$archive"
sha256sum "$archive" > "$archive.sha256"
python3 -c 'import pathlib,sys;p=pathlib.Path(sys.argv[1]);current=pathlib.Path(sys.argv[2]);archives=sorted(p.glob("plugboard-*.tar.gz.age"))
for old in archives[:-7]:
 if old != current:
  old.unlink();old.with_suffix(old.suffix+".sha256").unlink(missing_ok=True)
' "$destination" "$archive"
printf '%s\n' "$archive"
