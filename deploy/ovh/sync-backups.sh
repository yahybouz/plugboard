#!/usr/bin/env bash
# Pull encrypted VPS backups to this Mac; no decryption key leaves the Mac.
# Usage: PLUGBOARD_BACKUP_SSH_TARGET=user@private-ip sync-backups.sh
set -euo pipefail
IFS=$'\n\t'
umask 077
target="${PLUGBOARD_BACKUP_SSH_TARGET:?Set the authorized private VPS SSH target}"
identity="${PLUGBOARD_SSH_IDENTITY:-$HOME/.ssh/plugboard_ovh}"
known_hosts="${PLUGBOARD_KNOWN_HOSTS:-$HOME/.config/plugboard/known_hosts}"
alias="${PLUGBOARD_HOST_KEY_ALIAS:?Set verified host-key alias}"
destination="${PLUGBOARD_BACKUP_DIRECTORY:-$HOME/.local/share/plugboard-backups}"
mkdir -p "$destination"
chmod 700 "$destination"
ssh_command="/usr/bin/ssh -i $identity -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o HostKeyAlias=$alias -o UserKnownHostsFile=$known_hosts -o ConnectTimeout=10 -o ServerAliveInterval=20"
/usr/bin/rsync -a --partial --timeout=60 \
  --include='*.tar.gz.age' --include='*.tar.gz.age.sha256' --exclude='*' \
  -e "$ssh_command" --rsync-path='sudo rsync' \
  "$target:/var/backups/plugboard/" "$destination/"
python3 -c 'import hashlib,pathlib,sys;p=pathlib.Path(sys.argv[1]);proofs=sorted(p.glob("plugboard-*.tar.gz.age.sha256"));assert proofs,"No backup checksums received"
for proof in proofs:
 line=proof.read_text().strip().split(); expected=line[0]; archive=p/pathlib.Path(line[-1]).name;assert archive.is_file();digest=hashlib.sha256()
 with archive.open("rb") as stream:
  for chunk in iter(lambda: stream.read(1024*1024),b""):digest.update(chunk)
 assert digest.hexdigest()==expected,archive.name
print("Encrypted backups verified:",len(proofs))
archives=sorted(p.glob("plugboard-*.tar.gz.age"))
for old in archives[:-30]:
 old.unlink();old.with_suffix(old.suffix+".sha256").unlink(missing_ok=True)
' "$destination"
