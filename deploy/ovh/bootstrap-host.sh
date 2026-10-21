#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 host; keep bootstrap SSH source-restricted.
# Usage: sudo bash bootstrap-host.sh <current-ssh-client-ip>
set -euo pipefail
IFS=$'\n\t'
[[ "${EUID}" -eq 0 ]] || exit 1
source_ip="${1:?SSH client IP required}"
python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$source_ip"
# shellcheck disable=SC1091  # Provided by the target Ubuntu host.
. /etc/os-release
[[ "${ID}" == ubuntu && "${VERSION_ID}" == 24.04 ]] || exit 1
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=600 update
apt-get -o DPkg::Lock::Timeout=600 install -y ca-certificates curl gnupg ufw age sqlite3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
python3 -c 'import pathlib,subprocess; arch=subprocess.check_output(["dpkg","--print-architecture"],text=True).strip(); pathlib.Path("/etc/apt/sources.list.d/docker.sources").write_text("Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: "+arch+"\nSigned-By: /etc/apt/keyrings/docker.asc\n")'
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
apt-get -o DPkg::Lock::Timeout=600 update
apt-get -o DPkg::Lock::Timeout=600 install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale
systemctl enable --now docker tailscaled
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  python3 -c 'from pathlib import Path;p=Path("/etc/fstab");s=p.read_text();line="/swapfile none swap sw 0 0\n";p.write_text(s+line) if "/swapfile " not in s else None'
fi
python3 -c 'from pathlib import Path;Path("/etc/sysctl.d/90-plugboard.conf").write_text("vm.swappiness=10\n")'
sysctl -p /etc/sysctl.d/90-plugboard.conf
python3 -c 'from pathlib import Path;Path("/etc/ssh/sshd_config.d/00-plugboard.conf").write_text("PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nX11Forwarding no\nAllowUsers ubuntu\n")'
sshd -t
systemctl reload ssh
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$source_ip" to any port 22 proto tcp comment 'plugboard-bootstrap-source-only'
ufw allow in on tailscale0 to any port 22 proto tcp comment 'plugboard-private-ssh'
ufw allow in on tailscale0 to any port 443 proto tcp comment 'plugboard-private-https'
ufw --force enable
install -d -m 0755 /opt/plugboard
install -d -m 0700 /etc/plugboard
python3 -c 'import pathlib,secrets,base64,os;p=pathlib.Path("/etc/plugboard/runtime.env");data={"ADMIN_KEY":secrets.token_urlsafe(48),"API_KEY":secrets.token_urlsafe(48),"ENCRYPTION_KEY":base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),"PLUGBOARD_ADMIN_MFA_REQUIRED":"1"};old=os.umask(0o077)
if not p.exists():
 with p.open("x") as f:f.write("".join(k+"="+v+"\n" for k,v in data.items()))
os.umask(old);os.chmod(p,0o600);print("Runtime secret file ready (values not printed)")'
docker version --format '{{.Server.Version}}'
docker compose version
tailscale version
ufw status verbose
sshd -T | python3 -c 'import sys;keys={"passwordauthentication","kbdinteractiveauthentication","permitrootlogin","allowusers"};print("".join(l for l in sys.stdin if l.split()[0] in keys),end="")'
printf 'HOST_BOOTSTRAP_OK\n'
